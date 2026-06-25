from werkzeug.utils import secure_filename
import os
import shutil
import csv
import io
import calendar
import base64
import json
from typing import Optional
from collections import defaultdict
from datetime import datetime, date, timedelta, time

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, current_app, abort, jsonify
from flask_login import login_required, current_user

from . import (
    APP_TIMEZONE,
    db,
    emit_header_notification_added,
    emit_header_notification_snapshot,
    format_reported_by_name,
    local_naive_to_localtime,
    mark_shared_ticket_comment_notifications_read,
    mark_task_notifications_read_for_user,
    mark_ticket_notifications_read_for_user,
    queue_ticket_comment_notifications,
    queue_task_comment_notifications,
    queue_task_completed_notifications,
    queue_ticket_creation_notifications,
    socketio,
    to_localtime,
    utc_naive_to_localtime,
)

from .models import (
    Ticket,
    Client,
    Instrument,
    TicketStatus,
    TicketPriority,
    TicketComment,
    TicketAttachment,
    TicketTask,
    TicketTaskWorkSession,
    TicketTaskAttachment,
    TicketTaskComment,
    UserRole,
    User,
    App,
    DeveloperPrompt,
    DeveloperPromptResponse,
    CommentTemplate,
)


tickets_bp = Blueprint("tickets", __name__, template_folder="templates")
CLIENT_SCOPED_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN)
READ_ONLY_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN, UserRole.SALES)
TASK_CATEGORY_PREFIX = "task:"
REACTION_OPTIONS = [
    {"code": "thumbs_up", "emoji": "\U0001F44D"},
    {"code": "heart", "emoji": "\u2764\ufe0f"},
    {"code": "laugh", "emoji": "\U0001F602"},
    {"code": "wow", "emoji": "\U0001F62E"},
    {"code": "sad", "emoji": "\U0001F622"},
    {"code": "angry", "emoji": "\U0001F621"},
]


@tickets_bp.before_request
def require_ticket_nav_access():
    if not current_user.is_authenticated:
        return
    task_endpoints = {
        "tickets.developer_tasks",
        "tickets.developer_prompt_history",
        "tickets.create_developer_prompt",
        "tickets.developer_prompt_state",
        "tickets.respond_developer_prompt",
        "tickets.task_detail",
        "tickets.update_task_info",
        "tickets.toggle_task_work_state",
        "tickets.update_task_kanban_status",
        "tickets.react_task_comment",
        "tickets.edit_task_comment",
        "tickets.delete_task_comment",
        "tickets.create_task",
        "tickets.task_workday_prompt_state",
        "tickets.task_workday_prompt_pause",
    }
    if request.endpoint in task_endpoints:
        if not (current_user.has_nav_access("developer_tasks") or current_user.has_nav_access("my_tickets")):
            abort(403)
        return
    if request.endpoint == "tickets.developer_workload":
        if not current_user.has_nav_access("developer_workload"):
            abort(403)
        return
    if request.endpoint == "tickets.index":
        if not current_user.has_nav_access("tickets"):
            abort(403)
        return
    if request.endpoint == "tickets.my_tickets":
        if not current_user.has_nav_access("my_tickets"):
            abort(403)
        return
    if not (current_user.has_nav_access("tickets") or current_user.has_nav_access("my_tickets")):
        abort(403)


def generate_ticket_no(ticket_id: int) -> str:
    # Pads numeric ID to 6 digits with prefix T-
    return f"T-{ticket_id:06d}"


def generate_task_ticket_no(ticket_id: int) -> str:
    return f"TT-{ticket_id:06d}"


def _task_category(parent_ticket_id: int) -> str:
    return f"{TASK_CATEGORY_PREFIX}{parent_ticket_id}"


def _is_task_ticket(ticket: Ticket) -> bool:
    if isinstance(ticket, TicketTask):
        return True
    return bool((ticket.category or "").startswith(TASK_CATEGORY_PREFIX))


def _exclude_task_tickets(query):
    return query.filter(db.or_(Ticket.category.is_(None), ~Ticket.category.like(f"{TASK_CATEGORY_PREFIX}%")))


def _apply_client_ticket_scope(query):
    if current_user.role == UserRole.CLIENT:
        return query.filter(
            Ticket.client_id == current_user.client_id,
            Ticket.reported_by_id == current_user.id,
        )
    if current_user.role == UserRole.CLIENT_ADMIN:
        return query.filter(Ticket.client_id == current_user.client_id)
    return query


def _ensure_client_ticket_access(ticket: Ticket) -> None:
    if current_user.role == UserRole.CLIENT:
        if ticket.client_id != current_user.client_id or ticket.reported_by_id != current_user.id:
            abort(403)
    elif current_user.role == UserRole.CLIENT_ADMIN:
        if ticket.client_id != current_user.client_id:
            abort(403)


def _timeline_timestamp(value, storage="guess"):
    if storage == "local":
        return local_naive_to_localtime(value)
    if storage == "utc":
        return utc_naive_to_localtime(value)
    return to_localtime(value)


def _ensure_ticket_not_closed(ticket: Ticket):
    if ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        flash("Closed or cancelled tickets are read-only.", "warning")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))
    return None


def _ensure_task_not_closed(task: TicketTask):
    if task.status == TicketStatus.CLOSED:
        flash("Closed tasks are read-only.", "warning")
        return redirect(url_for("tickets.task_detail", task_id=task.id))
    return None


def _update_ticket_status_value(ticket: Ticket, new_status, actor_name: str) -> bool:
    old_status = ticket.status
    if old_status == new_status:
        return False
    ticket.status = new_status
    if new_status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.REOPENED, TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED):
        ticket.kanban_bucket = None
    if new_status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        ticket.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    elif new_status == TicketStatus.REOPENED:
        ticket.closed_at = None
    _record_ticket_change(
        ticket,
        f"Status changed from {_enum_label(old_status)} to {_enum_label(new_status)} by {actor_name}.",
    )
    return True


def _update_task_status_value(task: TicketTask, new_status, actor_name: str) -> bool:
    old_status = task.status
    if old_status == new_status:
        return False
    task.status = new_status
    if new_status in (TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.REOPENED, TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED):
        task.kanban_bucket = None
    if new_status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        task.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    elif new_status == TicketStatus.REOPENED:
        task.closed_at = None
    _record_task_change(
        task,
        f"Status changed from {_enum_label(old_status)} to {_enum_label(new_status)} by {actor_name}.",
    )
    return True


def _close_child_tasks(parent_ticket: Ticket, actor_name: str):
    if _is_task_ticket(parent_ticket):
        return []

    closed_tasks = []
    closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    child_tasks = TicketTask.query.filter(TicketTask.ticket_id == parent_ticket.id).all()
    for child_task in child_tasks:
        if child_task.status == TicketStatus.CLOSED:
            continue
        old_status = child_task.status
        child_task.status = TicketStatus.CLOSED
        child_task.closed_at = closed_at
        child_task.is_working = False
        child_task.kanban_bucket = None
        _record_task_change(
            child_task,
            f"Status changed from {_enum_label(old_status)} to Closed by {actor_name}.",
        )
        closed_tasks.append(child_task)
    return closed_tasks


def _active_task_work_session(task: TicketTask):
    return TicketTaskWorkSession.query.filter(
        TicketTaskWorkSession.ticket_task_id == task.id,
        TicketTaskWorkSession.paused_at.is_(None),
        TicketTaskWorkSession.ended_at.is_(None),
    ).order_by(TicketTaskWorkSession.started_at.desc(), TicketTaskWorkSession.id.desc()).first()


def _latest_task_work_session(task: TicketTask):
    return TicketTaskWorkSession.query.filter(
        TicketTaskWorkSession.ticket_task_id == task.id,
    ).order_by(TicketTaskWorkSession.started_at.desc(), TicketTaskWorkSession.id.desc()).first()


def _task_work_session_state(task: TicketTask) -> str:
    if task.is_working or _active_task_work_session(task):
        return "working"
    latest_session = _latest_task_work_session(task)
    if latest_session and latest_session.paused_at and not latest_session.ended_at:
        return "paused"
    return "not_started"


def _current_user_working_tasks_query():
    active_session_task_ids = db.session.query(TicketTaskWorkSession.ticket_task_id).filter(
        TicketTaskWorkSession.developer_id == current_user.id,
        TicketTaskWorkSession.paused_at.is_(None),
        TicketTaskWorkSession.ended_at.is_(None),
    )
    return TicketTask.query.filter(
        TicketTask.assigned_engineer_id == current_user.id,
        db.or_(
            TicketTask.is_working.is_(True),
            TicketTask.id.in_(active_session_task_ids),
        ),
        TicketTask.status != TicketStatus.CLOSED,
        TicketTask.status != TicketStatus.CANCELLED,
    ).order_by(TicketTask.updated_at.desc(), TicketTask.created_at.desc(), TicketTask.id.desc())


def _current_workday_prompt_cutoff(now: Optional[datetime] = None) -> datetime:
    now = now or datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    today_cutoff = datetime.combine(now.date(), time(17, 30))
    if now >= today_cutoff:
        return today_cutoff
    return today_cutoff - timedelta(days=1)


def _task_is_eligible_for_workday_prompt(task: TicketTask, prompt_cutoff: datetime) -> bool:
    active_session = _active_task_work_session(task)
    if active_session and active_session.started_at:
        return active_session.started_at <= prompt_cutoff

    started_at = None
    if task.started_date:
        started_at = datetime.combine(task.started_date, time.min)
    elif task.created_at:
        started_at = task.created_at
    return bool(started_at and started_at <= prompt_cutoff)


def _can_view_task_detail(task: TicketTask) -> bool:
    if current_user.has_nav_access("developer_tasks"):
        return True
    if current_user.role == UserRole.ADMIN:
        return True
    if current_user.role == UserRole.ENGINEER:
        return any(
            user_id == current_user.id
            for user_id in (
                task.assigned_engineer_id,
                task.reported_by_id,
                task.assigned_by_id,
            )
        )
    return False


def _can_reassign_task() -> bool:
    return (
        current_user.role == UserRole.ADMIN
        or current_user.has_nav_access("developer_tasks")
        or current_user.has_nav_access("developer_workload")
    )


def _scoped_ticket_query():
    query = _exclude_task_tickets(Ticket.query)
    if current_user.role in CLIENT_SCOPED_ROLES:
        query = _apply_client_ticket_scope(query)
    elif current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
        query = query.filter(Ticket.assigned_engineer_id == current_user.id)
    return query


def _app_monitoring_rows():
    rows = []
    query = _scoped_ticket_query().filter(Ticket.ticket_for == "app")
    tickets = query.order_by(Ticket.created_at.desc()).all()
    grouped = {}
    for ticket in tickets:
        app_id = ticket.app_id or 0
        if app_id not in grouped:
            grouped[app_id] = {
                "app_id": ticket.app_id,
                "app_name": ticket.app.name if ticket.app else "Unassigned App",
                "open": 0,
                "in_progress": 0,
                "resolved": 0,
                "closed": 0,
                "total": 0,
                "last_updated_at": ticket.updated_at or ticket.created_at,
            }
        row = grouped[app_id]
        row["total"] += 1
        row["last_updated_at"] = max(
            row["last_updated_at"] or datetime.min,
            ticket.updated_at or ticket.created_at or datetime.min,
        )
        if ticket.status == TicketStatus.OPEN:
            row["open"] += 1
        elif ticket.status == TicketStatus.IN_PROGRESS:
            row["in_progress"] += 1
        elif ticket.status == TicketStatus.RESOLVED:
            row["resolved"] += 1
        elif ticket.status == TicketStatus.CLOSED:
            row["closed"] += 1
    rows = list(grouped.values())
    rows.sort(key=lambda row: (row["app_name"] or "").lower())
    return rows


def _ticket_event_payload(ticket) -> dict:
    is_task = isinstance(ticket, TicketTask)
    payload = {
        "id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "subject": ticket.subject,
        "status": ticket.status.value if ticket.status else "",
        "priority": ticket.priority.value if ticket.priority else "",
        "client": ticket.client.name if ticket.client else "",
        "updated_at": ticket.updated_at.strftime("%Y-%m-%d %H:%M:%S") if ticket.updated_at else "",
    }
    if is_task:
        payload["parent_ticket_id"] = ticket.ticket_id
    elif _is_task_ticket(ticket):
        try:
            payload["parent_ticket_id"] = int((ticket.category or "").replace(TASK_CATEGORY_PREFIX, "", 1))
        except ValueError:
            payload["parent_ticket_id"] = None
    else:
        payload["parent_ticket_id"] = None
    return payload


def _emit_ticket_changed(ticket: Ticket, action: str = "updated") -> None:
    payload = _ticket_event_payload(ticket)
    payload["action"] = action
    socketio.emit("ticket_changed", payload, room="tickets")
    socketio.emit("ticket_changed", payload, room=f"ticket:{ticket.id}")
    if payload.get("parent_ticket_id"):
        socketio.emit("ticket_changed", payload, room=f"ticket:{payload['parent_ticket_id']}")


def _ticket_attachment_payload(attachment: TicketAttachment) -> dict:
    endpoint = "uploads.task_file" if isinstance(attachment, TicketTaskAttachment) else "uploads.ticket_file"
    return {
        "id": attachment.id,
        "name": attachment.original_filename,
        "url": url_for(endpoint, filename=attachment.stored_filename),
        "content_type": attachment.content_type or "",
        "is_image": bool(attachment.content_type and attachment.content_type.startswith("image/")),
    }


def _clone_ticket_attachments(source_ticket: Ticket, target_ticket, uploaded_at=None) -> None:
    upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
    if not upload_folder:
        return

    os.makedirs(upload_folder, exist_ok=True)

    for source_attachment in source_ticket.attachments:
        source_path = os.path.join(upload_folder, source_attachment.stored_filename or "")
        if not source_attachment.stored_filename or not os.path.exists(source_path):
            continue

        safe_name = secure_filename(source_attachment.original_filename or os.path.basename(source_attachment.stored_filename))
        stored_name = f"{target_ticket.id}_{int(datetime.utcnow().timestamp() * 1000)}_{safe_name}"
        target_path = os.path.join(upload_folder, stored_name)
        shutil.copyfile(source_path, target_path)

        attachment_kwargs = {
            "user_id": source_attachment.user_id,
            "stored_filename": stored_name,
            "original_filename": source_attachment.original_filename,
            "content_type": source_attachment.content_type,
            "file_size": os.path.getsize(target_path),
            "uploaded_at": uploaded_at or source_attachment.uploaded_at,
        }
        if isinstance(target_ticket, TicketTask):
            db.session.add(TicketTaskAttachment(ticket_task_id=target_ticket.id, **attachment_kwargs))
        else:
            db.session.add(TicketAttachment(ticket_id=target_ticket.id, **attachment_kwargs))


def _clone_attachment_records(source_attachments, target_ticket, uploaded_at=None) -> None:
    upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
    if not upload_folder:
        return

    os.makedirs(upload_folder, exist_ok=True)

    for source_attachment in source_attachments:
        source_path = os.path.join(upload_folder, source_attachment.stored_filename or "")
        if not source_attachment.stored_filename or not os.path.exists(source_path):
            continue

        safe_name = secure_filename(source_attachment.original_filename or os.path.basename(source_attachment.stored_filename))
        stored_name = f"{target_ticket.id}_{int(datetime.utcnow().timestamp() * 1000)}_{safe_name}"
        target_path = os.path.join(upload_folder, stored_name)
        shutil.copyfile(source_path, target_path)

        attachment_kwargs = {
            "user_id": source_attachment.user_id,
            "stored_filename": stored_name,
            "original_filename": source_attachment.original_filename,
            "content_type": source_attachment.content_type,
            "file_size": os.path.getsize(target_path),
            "uploaded_at": uploaded_at or source_attachment.uploaded_at,
        }
        if isinstance(target_ticket, TicketTask):
            db.session.add(TicketTaskAttachment(ticket_task_id=target_ticket.id, **attachment_kwargs))
        else:
            db.session.add(TicketAttachment(ticket_id=target_ticket.id, **attachment_kwargs))


def _emit_ticket_comment(ticket: Ticket, comment: TicketComment, attachments=None) -> None:
    if comment.is_internal:
        _emit_ticket_changed(ticket, "commented")
        return
    socketio.emit("ticket_comment_added", _ticket_comment_payload(comment, attachments=attachments), room=f"ticket:{ticket.id}")
    _emit_ticket_comment_notification(ticket, comment)
    _emit_ticket_changed(ticket, "commented")


def _ticket_comment_payload(comment: TicketComment, attachments=None) -> dict:
    parent = comment.parent_comment
    user_is_provider = bool(
        comment.user
        and comment.user.role == UserRole.ENGINEER
        and (comment.user.user_type or "").strip().lower() in ("it", "support")
    )
    return {
        "ticket_id": comment.ticket_id,
        "comment_id": comment.id,
        "parent_comment": _ticket_comment_parent_payload(parent) if parent else None,
        "user_id": comment.user_id,
        "user": comment.user.full_name or comment.user.username if comment.user else "",
        "comment_text": comment.display_comment_text,
        "is_internal": bool(comment.is_internal),
        "deleted": bool(comment.deleted),
        "user_is_client": bool(comment.user and comment.user.role in CLIENT_SCOPED_ROLES),
        "user_is_provider": user_is_provider,
        "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
        "reactions": [] if comment.deleted else comment.reaction_summary(),
        "attachments": [] if comment.deleted else [_ticket_attachment_payload(att) for att in (attachments or [])],
    }


def _ticket_comment_parent_payload(comment: TicketComment) -> dict:
    return {
        "comment_id": comment.id,
        "user": comment.user.full_name or comment.user.username if comment.user else "",
        "comment_text": comment.display_comment_text,
        "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
    }


def _task_comment_payload(comment: TicketTaskComment, attachments=None) -> dict:
    parent = comment.parent_comment
    user_is_provider = bool(
        comment.user
        and comment.user.role == UserRole.ENGINEER
        and (comment.user.user_type or "").strip().lower() in ("it", "support")
    )
    return {
        "ticket_id": comment.ticket_task_id,
        "comment_id": comment.id,
        "parent_comment": _task_comment_parent_payload(parent) if parent else None,
        "user_id": comment.user_id,
        "user": comment.user.full_name or comment.user.username if comment.user else "",
        "comment_text": comment.display_comment_text,
        "is_internal": bool(comment.is_internal),
        "deleted": bool(comment.deleted),
        "user_is_client": bool(comment.user and comment.user.role in CLIENT_SCOPED_ROLES),
        "user_is_provider": user_is_provider,
        "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
        "reactions": [] if comment.deleted else comment.reaction_summary(),
        "attachments": [] if comment.deleted else [_ticket_attachment_payload(att) for att in (attachments or [])],
    }


def _task_comment_parent_payload(comment: TicketTaskComment) -> dict:
    return {
        "comment_id": comment.id,
        "user": comment.user.full_name or comment.user.username if comment.user else "",
        "comment_text": comment.display_comment_text,
        "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
    }


def _comment_reaction_payload(comment: TicketComment) -> dict:
    reaction_state_map = comment.reaction_state_map()
    my_state = reaction_state_map.get(str(current_user.id), {})
    return {
        "ticket_id": comment.ticket_id,
        "comment_id": comment.id,
        "reactions": comment.reaction_summary(),
        "my_reaction": my_state.get("reaction", ""),
        "my_acknowledged": bool(my_state.get("acknowledge")),
    }


def _task_comment_reaction_payload(comment: TicketTaskComment) -> dict:
    reaction_state_map = comment.reaction_state_map()
    return {
        "ticket_id": comment.ticket_task_id,
        "comment_id": comment.id,
        "reactions": comment.reaction_summary(),
        "my_reaction": reaction_state_map.get(str(current_user.id), {}).get("reaction", ""),
        "my_acknowledged": bool(reaction_state_map.get(str(current_user.id), {}).get("acknowledge")),
    }


def _is_support_user(user: User) -> bool:
    return bool(
        user
        and user.role == UserRole.ENGINEER
        and (user.user_type or "").strip().lower() == "support"
    )

def _can_manage_comment_templates(user: User) -> bool:
    return bool(
        user
        and (
            user.role == UserRole.ADMIN
            or (
                user.role == UserRole.ENGINEER
                and (user.user_type or "").strip().lower() == "support"
            )
        )
    )


def _comment_templates_for_user(user: User):
    if not _can_manage_comment_templates(user):
        return []

    return CommentTemplate.query.filter(
        CommentTemplate.is_active.is_(True),
        CommentTemplate.deleted_at.is_(None),
        db.or_(
            CommentTemplate.is_exclusive.is_(False),
            db.and_(
                CommentTemplate.is_exclusive.is_(True),
                CommentTemplate.created_by == user.id,
            ),
        ),
    ).order_by(CommentTemplate.created_at.desc()).all()

def _is_it_or_support_user(user: User) -> bool:
    return bool(
        user
        and user.role == UserRole.ENGINEER
        and (user.user_type or "").strip().lower() in {"it", "support"}
    )


def _can_prompt_client_resolution(user: User) -> bool:
    return bool(user and (user.role == UserRole.ADMIN or _is_it_or_support_user(user)))


def _can_manage_developer_prompts(user: User) -> bool:
    return bool(user and (user.role == UserRole.ADMIN or _is_it_or_support_user(user)))


def _can_create_tasks(user: User) -> bool:
    return bool(user and (user.role == UserRole.ADMIN or user.role == UserRole.ENGINEER))


def _developer_prompt_recipients(exclude_user_id: Optional[int] = None):
    query = User.query.filter(
        User.is_active_user.is_(True),
        User.role == UserRole.ENGINEER,
    ).order_by(User.full_name.asc(), User.username.asc())

    if exclude_user_id:
        query = query.filter(User.id != exclude_user_id)
    return query.all()


def _build_developer_prompt_title(message: str, raw_title: str = "") -> str:
    title = (raw_title or "").strip()
    if title:
        return title[:255]

    normalized = " ".join((message or "").strip().split())
    if not normalized:
        return "Developer Prompt"
    if len(normalized) <= 80:
        return normalized
    return normalized[:77].rstrip() + "..."


def _developer_prompt_payload_for_response(response: DeveloperPromptResponse) -> Optional[dict]:
    prompt = response.prompt if response else None
    creator = prompt.created_by if prompt else None
    if not prompt:
        return None

    creator_name = ""
    if creator:
        creator_name = creator.full_name or creator.username or ""

    return {
        "prompt_id": prompt.id,
        "title": prompt.title,
        "message": prompt.message,
        "created_at": to_localtime(prompt.created_at).strftime("%Y-%m-%d %H:%M") if prompt.created_at else "",
        "created_by": creator_name or "System",
        "response_status": (response.response_status or "pending").strip().lower(),
    }


def _pending_developer_prompt_state_for_user(user: User) -> dict:
    if not user or user.role != UserRole.ENGINEER:
        return {"count": 0, "prompt": None}

    pending_rows = (
        DeveloperPromptResponse.query
        .join(DeveloperPrompt, DeveloperPromptResponse.prompt_id == DeveloperPrompt.id)
        .filter(
            DeveloperPromptResponse.user_id == user.id,
            DeveloperPromptResponse.response_status == "pending",
        )
        .order_by(DeveloperPrompt.created_at.desc(), DeveloperPrompt.id.desc())
        .all()
    )
    if not pending_rows:
        return {"count": 0, "prompt": None}
    return {
        "count": len(pending_rows),
        "prompt": _developer_prompt_payload_for_response(pending_rows[0]),
    }


def _create_developer_prompt(creator: User, title: str, message: str):
    recipients = _developer_prompt_recipients(exclude_user_id=getattr(creator, "id", None))
    if not recipients:
        return None, 0

    prompt = DeveloperPrompt(
        created_by_id=creator.id,
        title=_build_developer_prompt_title(message, title),
        message=message.strip(),
        created_at=datetime.now(APP_TIMEZONE).replace(tzinfo=None),
    )
    db.session.add(prompt)
    db.session.flush()

    for recipient in recipients:
        db.session.add(
            DeveloperPromptResponse(
                prompt_id=prompt.id,
                user_id=recipient.id,
                response_status="pending",
            )
        )

    return prompt, len(recipients)


def _emit_developer_prompt(prompt: DeveloperPrompt) -> int:
    if not prompt:
        return 0

    sent = 0
    rows = (
        DeveloperPromptResponse.query
        .filter(DeveloperPromptResponse.prompt_id == prompt.id)
        .all()
    )
    for row in rows:
        payload = _developer_prompt_payload_for_response(row)
        if not payload:
            continue
        state = _pending_developer_prompt_state_for_user(row.user)
        socketio.emit(
            "developer_prompt",
            {
                "count": state.get("count", 0),
                "prompt": payload,
            },
            room=f"user_notifications:{row.user_id}",
        )
        sent += 1
    return sent


def _can_edit_core_ticket_fields(user: User) -> bool:
    return _is_support_user(user)


def _can_fully_edit_task_detail(user: Optional[User]) -> bool:
    return bool(
        user and (
            user.role == UserRole.ADMIN
            or _is_support_user(user)
            or (
                user.has_nav_access("developer_tasks")
                and user.has_nav_access("developer_workload")
            )
        )
    )


def _task_assignee_filters(user: Optional[User] = None):
    if not user:
        return [db.func.lower(User.user_type) == "support"]
    if user.role == UserRole.ADMIN or _is_support_user(user):
        return [db.true()]
    if user.role == UserRole.ENGINEER:
        return [User.id == user.id]
    return [db.func.lower(User.user_type) == "support"]


def _status_matches(column, raw_values):
    normalized = [value.strip().lower() for value in raw_values if value and value.strip()]
    if not normalized:
        return None
    return db.func.lower(db.cast(column, db.String)).in_(normalized)


def _priority_rank(priority: Optional[TicketPriority]) -> int:
    ranking = {
        TicketPriority.NOT_SET: 0,
        TicketPriority.LOW: 1,
        TicketPriority.MEDIUM: 2,
        TicketPriority.HIGH: 3,
        TicketPriority.CRITICAL: 4,
    }
    return ranking.get(priority or TicketPriority.NOT_SET, 0)


def _sync_parent_ticket_priority_from_tasks(parent_ticket: Optional[Ticket], actor_name: str) -> bool:
    if not parent_ticket or _is_task_ticket(parent_ticket):
        return False

    child_tasks = TicketTask.query.filter(TicketTask.ticket_id == parent_ticket.id).all()
    task_priorities = [
        task.priority
        for task in child_tasks
        if task.priority and task.priority != TicketPriority.NOT_SET
    ]
    if not task_priorities:
        return False

    highest_priority = max(task_priorities, key=_priority_rank)
    current_priority = parent_ticket.priority or TicketPriority.NOT_SET
    if current_priority == highest_priority:
        return False

    parent_ticket.priority = highest_priority
    _record_ticket_change(
        parent_ticket,
        f"Priority changed from {_enum_label(current_priority)} to {_enum_label(highest_priority)} by {actor_name}.",
    )
    return True


def _emit_comment_reaction(comment: TicketComment) -> None:
    socketio.emit(
        "ticket_comment_reacted",
        {
            "ticket_id": comment.ticket_id,
            "comment_id": comment.id,
            "reactions": comment.reaction_summary(),
        },
        room=f"ticket:{comment.ticket_id}",
    )
    _emit_ticket_comment_notification_count(comment)


def _is_ticket_comment_notification_for_user(comment: TicketComment, user: User) -> bool:
    if not comment or not user or comment.is_internal or comment.deleted:
        return False
    if comment.user_id == user.id:
        return False
    if _is_change_comment(comment.comment_text):
        return False

    ticket = comment.ticket
    if not ticket or ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return False

    commenter_is_client = bool(comment.user and comment.user.role in CLIENT_SCOPED_ROLES)
    viewer_is_client = user.role in CLIENT_SCOPED_ROLES
    if _is_support_user(user):
        return commenter_is_client
    if commenter_is_client == viewer_is_client:
        return False

    if user.role == UserRole.CLIENT:
        return ticket.client_id == user.client_id and ticket.reported_by_id == user.id
    if user.role == UserRole.CLIENT_ADMIN:
        return ticket.client_id == user.client_id and ticket.reported_by_id == user.id
    if user.role == UserRole.ENGINEER:
        return ticket.assigned_engineer_id == user.id
    if user.role == UserRole.SALES:
        return bool(ticket.client and ticket.client.assigned_sales_id == user.id)
    return False


def _notification_recipients_for_comment(ticket: Ticket, comment: TicketComment):
    if not comment.user or not ticket or ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return []

    user_ids = set()
    commenter_is_client = comment.user.role in CLIENT_SCOPED_ROLES
    if not commenter_is_client and ticket.reported_by and ticket.reported_by.role in CLIENT_SCOPED_ROLES:
        user_ids.add(ticket.reported_by_id)

    user_ids.discard(comment.user_id)
    if not user_ids:
        return []
    recipients = User.query.filter(User.id.in_(user_ids)).all()
    return [
        recipient
        for recipient in recipients
        if _is_ticket_comment_notification_for_user(comment, recipient)
    ]


def _can_modify_ticket_comment(comment: TicketComment) -> bool:
    if not comment or comment.deleted or _is_change_comment(comment.comment_text):
        return False
    return comment.user_id == current_user.id


def _can_modify_task_comment(comment: TicketTaskComment) -> bool:
    if not comment or comment.deleted or _is_change_comment(comment.comment_text):
        return False
    return comment.user_id == current_user.id


def _ticket_comment_notification_count_for_user(user: User) -> int:
    if not user:
        return 0
    if user.role == UserRole.ADMIN or _is_support_user(user):
        return 0

    query = (
        TicketComment.query
        .join(Ticket, TicketComment.ticket_id == Ticket.id)
        .join(User, TicketComment.user_id == User.id)
        .filter(
            TicketComment.is_internal.is_(False),
            TicketComment.deleted.is_(False),
            TicketComment.user_id != user.id,
        )
        .filter(Ticket.status.notin_([TicketStatus.CLOSED, TicketStatus.CANCELLED]))
    )

    if _is_support_user(user):
        query = query.filter(User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))
    elif user.role in CLIENT_SCOPED_ROLES:
        query = query.filter(~User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))
        query = query.filter(Ticket.client_id == user.client_id, Ticket.reported_by_id == user.id)
    else:
        query = query.filter(User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))

    if _is_support_user(user):
        pass
    elif user.role == UserRole.ENGINEER:
        query = query.filter(Ticket.assigned_engineer_id == user.id)
    elif user.role == UserRole.SALES:
        query = query.join(Client, Ticket.client_id == Client.id).filter(Client.assigned_sales_id == user.id)
    elif user.role == UserRole.ADMIN:
        return 0

    unread_ticket_ids = set()
    for comment in query.order_by(TicketComment.created_at.desc()).limit(100).all():
        if not _is_ticket_comment_notification_for_user(comment, user):
            continue
        if comment.reaction_state_map().get(str(user.id), {}).get("acknowledge"):
            continue
        unread_ticket_ids.add(comment.ticket_id)
    return len(unread_ticket_ids)


def _ticket_comment_notification_snapshot_for_user(user: User, limit: int = 8) -> dict:
    if not user:
        return {"count": 0, "notifications": []}
    if user.role == UserRole.ADMIN or _is_support_user(user):
        return {"count": 0, "notifications": []}

    query = (
        TicketComment.query
        .join(Ticket, TicketComment.ticket_id == Ticket.id)
        .join(User, TicketComment.user_id == User.id)
        .filter(
            TicketComment.is_internal.is_(False),
            TicketComment.deleted.is_(False),
            TicketComment.user_id != user.id,
        )
        .filter(Ticket.status.notin_([TicketStatus.CLOSED, TicketStatus.CANCELLED]))
    )

    if _is_support_user(user):
        query = query.filter(User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))
    elif user.role in CLIENT_SCOPED_ROLES:
        query = query.filter(~User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))
        query = query.filter(Ticket.client_id == user.client_id, Ticket.reported_by_id == user.id)
    else:
        query = query.filter(User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))

    if _is_support_user(user):
        pass
    elif user.role == UserRole.ENGINEER:
        query = query.filter(Ticket.assigned_engineer_id == user.id)
    elif user.role == UserRole.SALES:
        query = query.join(Client, Ticket.client_id == Client.id).filter(Client.assigned_sales_id == user.id)
    elif user.role == UserRole.ADMIN:
        return {"count": 0, "notifications": []}

    by_ticket = {}
    for comment in query.order_by(TicketComment.created_at.desc()).limit(100).all():
        if not _is_ticket_comment_notification_for_user(comment, user):
            continue
        if comment.reaction_state_map().get(str(user.id), {}).get("acknowledge"):
            continue
        if comment.ticket_id not in by_ticket:
            by_ticket[comment.ticket_id] = {
                "ticket": comment.ticket,
                "latest_comment": comment,
                "count": 0,
            }
        by_ticket[comment.ticket_id]["count"] += 1

    notifications = []
    for entry in list(by_ticket.values())[:limit]:
        ticket = entry["ticket"]
        comment = entry["latest_comment"]
        if not ticket or ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
            continue
        notifications.append({
            "ticket_id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "subject": ticket.subject,
            "reported_by": ticket.reported_by.full_name or ticket.reported_by.username if ticket.reported_by else "",
            "url": url_for("tickets.detail", ticket_id=ticket.id) + "#ticket-comments-card",
            "comment_id": comment.id,
            "comment_text": comment.comment_text,
            "user": comment.user.full_name or comment.user.username if comment.user else "",
            "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
            "count": entry["count"],
        })

    return {
        "count": len(by_ticket),
        "notifications": notifications,
    }


def _header_notification_payload(comment: TicketComment, recipient: User) -> dict:
    ticket = comment.ticket
    return {
        "count": _ticket_comment_notification_count_for_user(recipient),
        "notification": {
            "ticket_id": ticket.id,
            "ticket_no": ticket.ticket_no,
            "subject": ticket.subject,
            "reported_by": ticket.reported_by.full_name or ticket.reported_by.username if ticket.reported_by else "",
            "url": url_for("tickets.detail", ticket_id=ticket.id) + "#ticket-comments-card",
            "comment_id": comment.id,
            "comment_text": comment.comment_text,
            "user": comment.user.full_name or comment.user.username if comment.user else "",
            "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
        },
    }


def _emit_ticket_comment_notification(ticket: Ticket, comment: TicketComment) -> None:
    queued_notifications = queue_ticket_comment_notifications(ticket, comment)
    if queued_notifications:
        for recipient, notification in queued_notifications:
            emit_header_notification_added(recipient, notification)


def _emit_ticket_comment_notification_count(comment: TicketComment) -> None:
    recipients = {}

    for user in _notification_recipients_for_comment(comment.ticket, comment):
        recipients[user.id] = user

    if _is_ticket_comment_notification_for_user(comment, current_user):
        recipients[current_user.id] = current_user

    for recipient in recipients.values():
        socketio.emit(
            "ticket_comment_notification_count",
            {"count": _ticket_comment_notification_count_for_user(recipient)},
            room=f"user_notifications:{recipient.id}",
        )
        socketio.emit(
            "ticket_comment_notification_snapshot",
            _ticket_comment_notification_snapshot_for_user(recipient),
            room=f"user_notifications:{recipient.id}",
        )


def _resolved_tickets_pending_for_user(user: User, client_id: Optional[int] = None, client_ids: Optional[list] = None):
    if not user or user.role not in CLIENT_SCOPED_ROLES:
        return []

    query = _exclude_task_tickets(Ticket.query).filter(Ticket.status == TicketStatus.RESOLVED)
    if user.role == UserRole.CLIENT:
        query = query.filter(
            Ticket.client_id == user.client_id,
            Ticket.reported_by_id == user.id,
        )
    else:
        query = query.filter(Ticket.client_id == user.client_id)

    if client_ids:
        query = query.filter(Ticket.client_id.in_(client_ids))
    elif client_id is not None:
        query = query.filter(Ticket.client_id == client_id)

    return query.order_by(Ticket.closed_at.desc(), Ticket.updated_at.desc(), Ticket.id.desc()).all()


def _client_resolution_prompt_recipients(ticket: Optional[Ticket] = None, client_ids: Optional[list] = None):
    query = User.query.filter(
        User.is_active_user.is_(True),
        User.role.in_(CLIENT_SCOPED_ROLES),
    )

    if ticket is not None:
        query = query.filter(User.client_id == ticket.client_id)
    elif client_ids:
        query = query.filter(User.client_id.in_(client_ids))
    return query.order_by(User.full_name.asc(), User.username.asc()).all()


def _client_resolution_prompt_payload(
    recipient: User,
    sender: Optional[User] = None,
    client_id: Optional[int] = None,
    client_ids: Optional[list] = None,
    tickets: Optional[list] = None,
) -> Optional[dict]:
    sender_name = ""
    if sender:
        sender_name = sender.full_name or sender.username or ""

    resolved_tickets = tickets if tickets is not None else _resolved_tickets_pending_for_user(
        recipient,
        client_id=client_id,
        client_ids=client_ids,
    )
    if not resolved_tickets:
        return None

    ticket_payloads = []
    for ticket in resolved_tickets:
        ticket_payloads.append(
            {
                "ticket_id": ticket.id,
                "ticket_no": ticket.ticket_no,
                "status": "Fix/Completed",
                "url": url_for("tickets.detail", ticket_id=ticket.id),
            }
        )

    return {
        "sender": sender_name,
        "count": len(ticket_payloads),
        "tickets": ticket_payloads,
        "message": (
            f"{sender_name or 'Support'} asked you to review your Fix/Completed ticket"
            f"{'' if len(ticket_payloads) == 1 else 's'}. Please Accept or Deny "
            f"{'it' if len(ticket_payloads) == 1 else 'them'}."
        ),
    }


def _emit_client_resolution_prompt(ticket: Ticket, sender: Optional[User] = None) -> int:
    recipients = _client_resolution_prompt_recipients(ticket)
    sent = 0
    for recipient in recipients:
        if recipient.role == UserRole.CLIENT and ticket.reported_by_id != recipient.id:
            continue
        payload = _client_resolution_prompt_payload(recipient, sender=sender, tickets=[ticket])
        if not payload:
            continue
        socketio.emit(
            "ticket_resolution_prompt",
            payload,
            room=f"user_notifications:{recipient.id}",
        )
        sent += 1
    return sent


def _emit_global_client_resolution_prompts(sender: Optional[User] = None, client_ids: Optional[list] = None) -> int:
    sent = 0
    for recipient in _client_resolution_prompt_recipients(client_ids=client_ids):
        payload = _client_resolution_prompt_payload(recipient, sender=sender, client_ids=client_ids)
        if not payload:
            continue
        socketio.emit(
            "ticket_resolution_prompt",
            payload,
            room=f"user_notifications:{recipient.id}",
        )
        sent += 1
    return sent


def _record_ticket_change(ticket: Ticket, message: str, is_internal: bool = False) -> None:
    """Create a lightweight timeline entry as a ticket comment."""
    comment = TicketComment(
        ticket_id=ticket.id,
        user_id=current_user.id,
        comment_text=message,
        is_internal=is_internal,
    )
    db.session.add(comment)


def _record_task_change(task: TicketTask, message: str, is_internal: bool = False) -> None:
    comment = TicketTaskComment(
        ticket_task_id=task.id,
        user_id=current_user.id,
        comment_text=message,
        is_internal=is_internal,
    )
    db.session.add(comment)


def _task_fix_completed_at(task: TicketTask):
    if not task:
        return None
    if task.status == TicketStatus.RESOLVED:
        return task.closed_at or task.updated_at or task.created_at

    latest_resolved_comment = max(
        (
            comment.created_at
            for comment in (task.comments or [])
            if comment.created_at and " to Fix/Completed by " in (comment.comment_text or "")
        ),
        default=None,
    )
    return latest_resolved_comment


def _enum_label(value) -> str:
    return value.value.replace("_", " ").title() if value else "N/A"


CHANGE_PREFIXES = (
    "Status changed",
    "Priority changed",
    "Target schedule changed",
    "Date needed changed",
    "Complaint/details updated",
    "Engineer/IT assigned",
    "Engineer/IT assignment cleared",
    "Work status changed",
)


def _is_change_comment(text: str) -> bool:
    text = text or ""
    return any(text.startswith(prefix) for prefix in CHANGE_PREFIXES)


def _hide_change_comment_for_viewer(comment_text: str) -> bool:
    text = comment_text or ""
    if current_user.role in CLIENT_SCOPED_ROLES and text.startswith("Priority changed from "):
        return True
    return False


@tickets_bp.route("/")
@login_required
def index():
    return _render_ticket_index(my_tickets_only=False)


@tickets_bp.route("/my")
@login_required
def my_tickets():
    return _render_ticket_index(my_tickets_only=True)


def _apply_my_ticket_scope(query):
    if current_user.role == UserRole.CLIENT:
        return query.filter(
            Ticket.client_id == current_user.client_id,
            Ticket.reported_by_id == current_user.id,
        )
    if current_user.role == UserRole.CLIENT_ADMIN:
        return query.filter(Ticket.client_id == current_user.client_id)
    if current_user.role == UserRole.ENGINEER:
        return query.filter(Ticket.assigned_engineer_id == current_user.id)
    if current_user.role == UserRole.SALES:
        return query.join(Client, Ticket.client_id == Client.id).filter(Client.assigned_sales_id == current_user.id)
    return query.filter(
        db.or_(
            Ticket.reported_by_id == current_user.id,
            Ticket.assigned_engineer_id == current_user.id,
            Ticket.assigned_by_id == current_user.id,
        )
    )


def _apply_my_task_scope(query):
    user_type = (current_user.user_type or "").strip().lower()
    if current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
        return query.filter(TicketTask.assigned_engineer_id == current_user.id)
    if _is_support_user(current_user):
        return query.filter(TicketTask.assigned_engineer_id == current_user.id)
    if current_user.role == UserRole.ADMIN or user_type == "administrator":
        return query.filter(TicketTask.assigned_engineer_id == current_user.id)
    return query.filter(
        db.or_(
            TicketTask.reported_by_id == current_user.id,
            TicketTask.assigned_engineer_id == current_user.id,
            TicketTask.assigned_by_id == current_user.id,
        )
    )


def _render_ticket_index(my_tickets_only=False):
    status_labels = {
        TicketStatus.OPEN: "Open",
        TicketStatus.IN_PROGRESS: "In-Process",
        TicketStatus.RESOLVED: "Fix/Completed",
        TicketStatus.REOPENED: "Re-Open",
        TicketStatus.CLOSED: "Closed",
        TicketStatus.ON_HOLD: "On Hold",
        TicketStatus.CANCELLED: "Cancelled",
    }

    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    apps = App.query.order_by(App.name.asc()).all()
    assignees = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc(), User.username.asc()).all()
    reporters = User.query.order_by(User.full_name.asc(), User.username.asc()).all()

    list_model = TicketTask if my_tickets_only and current_user.role not in CLIENT_SCOPED_ROLES else Ticket
    is_task_list = list_model is TicketTask
    can_create_task = _can_create_tasks(current_user)
    query = TicketTask.query if is_task_list else _exclude_task_tickets(Ticket.query)

    client_ids = request.args.getlist("client_id")
    client_ids = [cid.strip() for cid in client_ids if cid.strip()]

    instrument_ids = [iid.strip() for iid in request.args.getlist("instrument_id") if iid.strip()]
    app_ids = [aid.strip() for aid in request.args.getlist("app_id") if aid.strip()]
    ticket_no_raw = (request.args.get("ticket_no") or "").strip()
    # Assigned Engineer/IT filter hidden on the Tickets page.
    assignee_id = ""
    reported_by_ids = [rid.strip() for rid in request.args.getlist("reported_by_id") if rid.strip()]
    priority_values = [priority.strip() for priority in request.args.getlist("priority") if priority.strip()]
    status_values = [status.strip() for status in request.args.getlist("status") if status.strip()]
    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""
    search_text_raw = (request.args.get("search_text") or "").strip()
    using_default_open_not_set_scope = (
        current_user.role not in CLIENT_SCOPED_ROLES
        and not my_tickets_only
        and not search_text_raw
        and not ticket_no_raw
        and not priority_values
        and not status_values
    )
    effective_priority_values = list(priority_values)
    effective_status_values = list(status_values)

    if current_user.role in CLIENT_SCOPED_ROLES:
        instrument_ids = []
        app_ids = []
        assignee_id = ""
        reported_by_ids = []
        priority_values = []
        client_ids = [str(current_user.client_id)]
        query = _apply_client_ticket_scope(query)
        instruments = Instrument.query.filter(
            Instrument.client_id == current_user.client_id
        ).order_by(Instrument.name.asc()).all()
        apps = App.query.join(Client.apps).filter(
            Client.id == current_user.client_id
        ).order_by(App.name.asc()).all()
        reporters_query = User.query.filter(User.client_id == current_user.client_id)
        if current_user.role == UserRole.CLIENT:
            reporters_query = reporters_query.filter(User.id == current_user.id)
        reporters = reporters_query.order_by(User.full_name.asc(), User.username.asc()).all()
        clients = [c for c in clients if c.id == current_user.client_id]

    elif current_user.role == UserRole.ENGINEER and not _is_support_user(current_user) and (current_user.user_type or "").lower() != "it":
        assignee_id = ""
        query = query.filter(Ticket.assigned_engineer_id == current_user.id)
    
    elif client_ids:
        try:
            client_ids_int = [int(cid) for cid in client_ids]
            query = query.filter(list_model.client_id.in_(client_ids_int))
            instruments = Instrument.query.filter(
                Instrument.client_id.in_(client_ids_int)
            ).order_by(Instrument.name.asc()).all()
            apps = App.query.join(Client.apps).filter(
                Client.id.in_(client_ids_int)
            ).distinct().order_by(App.name.asc()).all()
            reporters = User.query.filter(
                db.or_(User.client_id.in_(client_ids_int), User.client_id.is_(None))
            ).order_by(User.full_name.asc(), User.username.asc()).all()
        except ValueError:
            pass

    if my_tickets_only:
        query = _apply_my_task_scope(query) if is_task_list else _apply_my_ticket_scope(query)

    if instrument_ids:
        try:
            query = query.filter(list_model.instrument_id.in_([int(iid) for iid in instrument_ids]))
        except ValueError:
            pass

    if app_ids:
        try:
            query = query.filter(list_model.app_id.in_([int(aid) for aid in app_ids]))
        except ValueError:
            pass

    if ticket_no_raw:
        ticket_no_column = TicketTask.task_no if is_task_list else Ticket.ticket_no
        query = query.filter(ticket_no_column.ilike(f"%{ticket_no_raw}%"))

    if search_text_raw:
        search_like = f"%{search_text_raw}%"
        if is_task_list:
            query = query.filter(
                db.or_(
                    TicketTask.task_no.ilike(search_like),
                    TicketTask.subject.ilike(search_like),
                    TicketTask.description.ilike(search_like),
                    TicketTask.client.has(
                        db.or_(
                            Client.name.ilike(search_like),
                            Client.client_code.ilike(search_like),
                        )
                    ),
                    TicketTask.instrument.has(Instrument.name.ilike(search_like)),
                    TicketTask.app.has(App.name.ilike(search_like)),
                    TicketTask.reported_by.has(
                        db.or_(
                            User.full_name.ilike(search_like),
                            User.username.ilike(search_like),
                        )
                    ),
                    TicketTask.assigned_engineer.has(
                        db.or_(
                            User.full_name.ilike(search_like),
                            User.username.ilike(search_like),
                        )
                    ),
                )
            )
        else:
            query = query.filter(
                db.or_(
                    Ticket.ticket_no.ilike(search_like),
                    Ticket.subject.ilike(search_like),
                    Ticket.description.ilike(search_like),
                    Ticket.client.has(
                        db.or_(
                            Client.name.ilike(search_like),
                            Client.client_code.ilike(search_like),
                        )
                    ),
                    Ticket.app.has(App.name.ilike(search_like)),
                    Ticket.reported_by.has(
                        db.or_(
                            User.full_name.ilike(search_like),
                            User.username.ilike(search_like),
                        )
                    ),
                    Ticket.assigned_engineer.has(
                        db.or_(
                            User.full_name.ilike(search_like),
                            User.username.ilike(search_like),
                        )
                    ),
                )
            )

    # Assigned Engineer/IT filter hidden on the Tickets page.
    # if assignee_id:
    #     if assignee_id == "unassigned":
    #         query = query.filter(Ticket.assigned_engineer_id.is_(None))
    #     else:
    #         query = query.filter(Ticket.assigned_engineer_id == int(assignee_id))

    if reported_by_ids:
        try:
            query = query.filter(list_model.reported_by_id.in_([int(rid) for rid in reported_by_ids]))
        except ValueError:
            pass

    valid_priority_values = []
    for priority_raw in effective_priority_values:
        try:
            valid_priority_values.append(TicketPriority(priority_raw))
        except ValueError:
            continue

    if using_default_open_not_set_scope:
        query = query.filter(
            list_model.priority == TicketPriority.NOT_SET,
            list_model.status == TicketStatus.OPEN,
        )
    else:
        if valid_priority_values:
            query = query.filter(list_model.priority.in_(valid_priority_values))

        status_filter = _status_matches(list_model.status, effective_status_values)
        if status_filter is not None:
            query = query.filter(status_filter)
        else:
            query = query.filter(~_status_matches(list_model.status, ["closed", "cancelled"]))

    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
            query = query.filter(list_model.created_at >= date_from)
        except ValueError:
            pass

    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d")
            date_to = date_to + timedelta(days=1)
            query = query.filter(list_model.created_at < date_to)
        except ValueError:
            pass

    if current_user.role in CLIENT_SCOPED_ROLES and not is_task_list:
        pending_first = db.case(
            (list_model.status == TicketStatus.RESOLVED, 0),
            else_=1,
        )
        tickets = query.order_by(
            pending_first.asc(),
            list_model.created_at.desc(),
            list_model.id.desc(),
        ).all()
    elif my_tickets_only:
        tickets = query.order_by(list_model.created_at.desc()).all()
    else:
        tickets = query.order_by(list_model.created_at.asc()).all()
    task_work_status_map = {}
    task_fix_completed_at_map = {}
    if is_task_list:
        task_work_status_map = {
            task.id: _task_work_session_state(task)
            for task in tickets
        }
        task_fix_completed_at_map = {
            task.id: _task_fix_completed_at(task)
            for task in tickets
        }
    now = datetime.now(APP_TIMEZONE)
    current_date = now.strftime("%Y-%m-%d")

    return render_template(
        "tickets/index.html",
        tickets=tickets,
        now=now,
        current_date=current_date,
        TicketStatus=TicketStatus,
        status_labels=status_labels,
        clients=clients,
        instruments=instruments,
        apps=apps,
        assignees=assignees,
        reporters=reporters,
        ticket_list_title="My Task" if my_tickets_only else "All Tickets",
        ticket_list_endpoint="tickets.my_tickets" if my_tickets_only else "tickets.index",
        is_task_list=is_task_list,
        task_work_status_map=task_work_status_map,
        task_fix_completed_at_map=task_fix_completed_at_map,
        can_create_task=can_create_task,
        selected_filters={
            "client_ids": client_ids,
            "instrument_ids": instrument_ids,
            "app_ids": app_ids,
            "search_text": search_text_raw,
            "ticket_no": ticket_no_raw,
            "assignee_id": assignee_id,
            "reported_by_ids": reported_by_ids,
            "priority": [] if using_default_open_not_set_scope else priority_values,
            "status": [] if using_default_open_not_set_scope else [status.strip().lower() for status in effective_status_values if status.strip()],
            "date_from": date_from_raw,
            "date_to": date_to_raw,
        },
    )


@tickets_bp.route("/app-monitoring")
@login_required
def app_monitoring():
    if current_user.role != UserRole.ADMIN:
        abort(403)
    rows = _app_monitoring_rows()
    return render_template(
        "tickets/app_monitoring.html",
        rows=rows,
        now=datetime.utcnow(),
    )


@tickets_bp.route("/app-monitoring/data")
@login_required
def app_monitoring_data():
    if current_user.role != UserRole.ADMIN:
        abort(403)
    rows = _app_monitoring_rows()
    payload = []
    for row in rows:
        payload.append(
            {
                "app_id": row["app_id"],
                "app_name": row["app_name"],
                "open": row["open"],
                "in_progress": row["in_progress"],
                "resolved": row["resolved"],
                "closed": row["closed"],
                "total": row["total"],
                "last_updated_at": to_localtime(row["last_updated_at"]).strftime("%Y-%m-%d %H:%M")
                if row["last_updated_at"]
                else "",
            }
        )
    return jsonify({"rows": payload})


@tickets_bp.route("/kanban")
@login_required
def kanban():
    status_labels = {
        TicketStatus.OPEN: "Open",
        TicketStatus.IN_PROGRESS: "In-Process",
        TicketStatus.RESOLVED: "Fix/Completed",
        TicketStatus.REOPENED: "Re-Open",
        TicketStatus.CLOSED: "Closed",
        TicketStatus.ON_HOLD: "On Hold",
        TicketStatus.CANCELLED: "Cancelled",
    }

    scope = request.args.get("scope", "").strip().lower()
    is_task_kanban = scope in ("tasks", "my_tasks")
    filter_by = request.args.get("filter_by", "").strip()
    filter_value = request.args.get("filter_value", "").strip()
    filter_user_type = request.args.get("filter_user_type", "").strip().upper()

    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    clients = Client.query.order_by(Client.name.asc()).all()

    if is_task_kanban:
        base_query = TicketTask.query
        if scope == "my_tasks" or (current_user.role == UserRole.ENGINEER and not _is_support_user(current_user)):
            base_query = _apply_my_task_scope(base_query)
            filter_by = "my"
            filter_value = ""
            filter_user_type = ""
    else:
        base_query = Ticket.query
        if current_user.role in READ_ONLY_ROLES:
            base_query = _exclude_task_tickets(base_query)

        if current_user.role in CLIENT_SCOPED_ROLES:
            base_query = _apply_client_ticket_scope(base_query)
            clients = [c for c in clients if c.id == current_user.client_id]
        elif current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
            base_query = base_query.filter(Ticket.assigned_engineer_id == current_user.id)
            filter_by = "my"
            filter_value = ""
            filter_user_type = ""

    assigned_column = TicketTask.assigned_engineer_id if is_task_kanban else Ticket.assigned_engineer_id
    client_column = TicketTask.client_id if is_task_kanban else Ticket.client_id
    created_column = TicketTask.created_at if is_task_kanban else Ticket.created_at
    assigned_relation = TicketTask.assigned_engineer if is_task_kanban else Ticket.assigned_engineer

    if filter_by == "my" and not (is_task_kanban and scope == "my_tasks"):
        base_query = base_query.filter(assigned_column == current_user.id)

    elif filter_by == "assigned":
        if filter_value == "unassigned":
            base_query = base_query.filter(assigned_column.is_(None))
        elif filter_value:
            try:
                base_query = base_query.filter(assigned_column == int(filter_value))
            except ValueError:
                pass

        if filter_user_type:
            base_query = base_query.join(assigned_relation).filter(
                db.func.upper(User.user_type) == filter_user_type
            )

    elif filter_by == "client":
        if filter_value:
            try:
                base_query = base_query.filter(client_column == int(filter_value))
            except ValueError:
                pass

    tickets = base_query.order_by(created_column.desc()).all()

    priority_order = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }

    def _sort_key(t):
        pval = t.priority.value if t.priority else ""
        created_ts = t.created_at.timestamp() if t.created_at else 0
        return (priority_order.get(pval, 99), -created_ts)

    tickets = sorted(tickets, key=_sort_key)

    today = datetime.utcnow().date()

    user_type = (current_user.user_type or "").lower()
    is_it = user_type == "it"

    backlog, currently_working, new_tickets, in_progress, completed, closed = [], [], [], [], [], []

    for t in tickets:
        is_overdue = t.target_date and t.target_date < today and t.status not in (
            TicketStatus.RESOLVED,
            TicketStatus.CLOSED,
            TicketStatus.CANCELLED,
        )

        if (t.kanban_bucket == "backlog" and t.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED)) or (
            is_overdue and t.status in (TicketStatus.OPEN, TicketStatus.REOPENED, TicketStatus.ON_HOLD)
        ):
            backlog.append(t)
            continue

        if is_it and getattr(t, "is_working", False):
            currently_working.append(t)

        if t.status == TicketStatus.OPEN:
            new_tickets.append(t)
        elif t.status in (TicketStatus.REOPENED, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD):
            in_progress.append(t)
        elif t.status == TicketStatus.RESOLVED:
            completed.append(t)
        elif t.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
            closed.append(t)
        else:
            in_progress.append(t)

    columns = [("Backlog", backlog), ("New Task", new_tickets)]

    columns.extend([
        ("In-Progress", in_progress),
        ("Completed", completed),
        ("Closed", closed),
    ])

    return render_template(
        "tickets/kanban.html",
        columns=columns,
        status_labels=status_labels,
        TicketStatus=TicketStatus,
        today=today,
        engineers=engineers,
        clients=clients,
        is_task_kanban=is_task_kanban,
        kanban_scope=scope,
        selected_filter={
            "filter_by": filter_by,
            "filter_value": filter_value,
            "filter_user_type": filter_user_type,
            "scope": scope,
        },
    )


@tickets_bp.route("/gantt")
@login_required
def gantt_view():
    scope = request.args.get("scope", "").strip().lower()
    is_task_gantt = scope in ("tasks", "my_tasks")
    group_by = request.args.get("group_by", "client")
    view_mode = request.args.get("view_mode", "month")
    start_raw = request.args.get("start")
    range_start_raw = request.args.get("date_from")
    range_end_raw = request.args.get("date_to")
    today_date = datetime.utcnow().date()

    def month_start(d: date) -> date:
        return date(d.year, d.month, 1)

    def add_months(d: date, months: int) -> date:
        month = d.month - 1 + months
        year = d.year + month // 12
        month = month % 12 + 1
        day = min(d.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def parse_date(raw, default):
        if not raw:
            return default
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return default

    base_start = parse_date(start_raw, today_date)

    if view_mode == "month":
        window_start = month_start(base_start)
        window_end = add_months(window_start, 1) - timedelta(days=1)
        nav_prev = add_months(window_start, -1)
        nav_next = add_months(window_start, 1)
    elif view_mode == "biweekly":
        window_start = base_start
        window_end = window_start + timedelta(days=13)
        nav_prev = window_start - timedelta(days=14)
        nav_next = window_start + timedelta(days=14)
    elif view_mode == "week":
        window_start = base_start
        window_end = window_start + timedelta(days=6)
        nav_prev = window_start - timedelta(days=7)
        nav_next = window_start + timedelta(days=7)
    elif view_mode == "range":
        range_start = parse_date(range_start_raw, base_start)
        range_end = parse_date(range_end_raw, range_start)
        if range_end < range_start:
            range_start, range_end = range_end, range_start
        window_start = range_start
        window_end = range_end
        nav_prev = window_start
        nav_next = window_start
    else:
        window_start = month_start(base_start)
        window_end = add_months(window_start, 1) - timedelta(days=1)
        nav_prev = add_months(window_start, -1)
        nav_next = add_months(window_start, 1)

    window_total_days = max((window_end - window_start).days + 1, 1)

    status_labels = {
        TicketStatus.OPEN: "Open",
        TicketStatus.IN_PROGRESS: "In-Process",
        TicketStatus.RESOLVED: "Fix/Completed",
        TicketStatus.REOPENED: "Re-Open",
        TicketStatus.CLOSED: "Closed",
        TicketStatus.ON_HOLD: "On Hold",
        TicketStatus.CANCELLED: "Cancelled",
    }

    if is_task_gantt:
        query = TicketTask.query
        if scope == "my_tasks" or (current_user.role == UserRole.ENGINEER and not _is_support_user(current_user)):
            query = _apply_my_task_scope(query)
    else:
        query = Ticket.query
        if current_user.role in READ_ONLY_ROLES:
            query = _exclude_task_tickets(query)
        if current_user.role in CLIENT_SCOPED_ROLES:
            query = _apply_client_ticket_scope(query)
        elif current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
            query = query.filter(Ticket.assigned_engineer_id == current_user.id)

    created_column = TicketTask.created_at if is_task_gantt else Ticket.created_at
    tickets = query.order_by(created_column.asc()).all()

    def grouping_label(ticket: Ticket) -> str:
        if group_by == "instrument":
            if ticket.ticket_for == "app":
                return ticket.app.name if ticket.app else "CDS App"
            return ticket.instrument.display_label() if ticket.instrument else "No Instrument"
        if group_by == "assignee":
            return ticket.assigned_engineer.full_name or ticket.assigned_engineer.username if ticket.assigned_engineer else "Unassigned"
        return ticket.client.name if ticket.client else "No Client"

    def priority_style(priority):
        if not priority:
            return ("#f1f5f9", "#6c757d")
        mapping = {
            TicketPriority.CRITICAL: ("#f8d7da", "#721c24"),
            TicketPriority.HIGH: ("#fff3cd", "#856404"),
            TicketPriority.MEDIUM: ("#e2f0fb", "#0c5460"),
            TicketPriority.LOW: ("#f1f3f5", "#495057"),
        }
        return mapping.get(priority, ("#f1f5f9", "#6c757d"))

    tasks = []
    for t in tickets:
        if not t.created_at:
            continue
        start_date = t.created_at.date()
        end_date = t.target_date or (t.closed_at.date() if t.closed_at else None) or start_date
        if end_date < start_date:
            end_date = start_date

        display_start = max(start_date, window_start)
        display_end = min(end_date, window_end)
        if display_end < window_start or display_start > window_end:
            continue

        bg_color, text_color = priority_style(t.priority)
        tasks.append(
            {
                "ticket": t,
                "start": start_date,
                "end": end_date,
                "display_start": display_start,
                "display_end": display_end,
                "status": t.status,
                "priority": t.priority,
                "group_label": grouping_label(t),
                "bg_color": bg_color,
                "text_color": text_color,
            }
        )

    day_headers = []
    current = window_start
    while current <= window_end:
        day_headers.append(
            {
                "date": current,
                "day": current.day,
                "dow": current.strftime("%a"),
                "month_label": current.strftime("%B %Y"),
            }
        )
        current += timedelta(days=1)

    month_spans = []
    if day_headers:
        current_label = day_headers[0]["month_label"]
        count = 0
        for d in day_headers:
            if d["month_label"] != current_label:
                month_spans.append({"label": current_label, "days": count})
                current_label = d["month_label"]
                count = 0
            count += 1
        month_spans.append({"label": current_label, "days": count})

    for task in tasks:
        start_offset = (task["display_start"] - window_start).days
        duration_days = max((task["display_end"] - task["display_start"]).days + 1, 1)
        task["left_pct"] = (start_offset / window_total_days) * 100
        task["width_pct"] = (duration_days / window_total_days) * 100

    groups_map = {}
    for task in tasks:
        label = task["group_label"]
        groups_map.setdefault(label, []).append(task)
    grouped_tasks = [{"label": k, "items_list": v} for k, v in sorted(groups_map.items(), key=lambda x: x[0].lower())]

    return render_template(
        "tickets/gantt.html",
        grouped_tasks=grouped_tasks,
        window_start=window_start,
        window_end=window_end,
        day_headers=day_headers,
        month_spans=month_spans,
        group_by=group_by,
        view_mode=view_mode,
        prev_start=nav_prev,
        next_start=nav_next,
        today_start=month_start(today_date),
        range_start_raw=range_start_raw,
        range_end_raw=range_end_raw,
        status_labels=status_labels,
        TicketStatus=TicketStatus,
        is_task_gantt=is_task_gantt,
        gantt_scope=scope,
    )


@tickets_bp.route("/calendar")
@login_required
def calendar_view():
    scope = request.args.get("scope", "").strip().lower()
    is_task_calendar = scope in ("tasks", "my_tasks")
    filter_by = request.args.get("filter_by") or ""
    filter_value = request.args.get("filter_value") or ""
    year_raw = request.args.get("year")
    month_raw = request.args.get("month")

    today = datetime.utcnow().date()
    try:
        year = int(year_raw) if year_raw else today.year
        month = int(month_raw) if month_raw else today.month
        if month < 1 or month > 12:
            raise ValueError
    except ValueError:
        year, month = today.year, today.month

    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    clients = Client.query.order_by(Client.name.asc()).all()

    if is_task_calendar:
        base_query = TicketTask.query
        if scope == "my_tasks" or (current_user.role == UserRole.ENGINEER and not _is_support_user(current_user)):
            base_query = _apply_my_task_scope(base_query)
            filter_by = "my"
            filter_value = ""
    else:
        base_query = Ticket.query
        if current_user.role in READ_ONLY_ROLES:
            base_query = _exclude_task_tickets(base_query)
        if current_user.role in CLIENT_SCOPED_ROLES:
            base_query = _apply_client_ticket_scope(base_query)
            clients = [c for c in clients if c.id == current_user.client_id]
        elif current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
            base_query = base_query.filter(Ticket.assigned_engineer_id == current_user.id)
            filter_by = "my"
            filter_value = ""

    assigned_column = TicketTask.assigned_engineer_id if is_task_calendar else Ticket.assigned_engineer_id
    client_column = TicketTask.client_id if is_task_calendar else Ticket.client_id
    target_column = TicketTask.target_date if is_task_calendar else Ticket.target_date

    if filter_by == "my" and not (is_task_calendar and scope == "my_tasks"):
        base_query = base_query.filter(assigned_column == current_user.id)
    elif filter_by == "engineer" and filter_value:
        if filter_value == "unassigned":
            base_query = base_query.filter(assigned_column.is_(None))
        else:
            try:
                base_query = base_query.filter(assigned_column == int(filter_value))
            except ValueError:
                pass
    elif filter_by == "client" and filter_value:
        try:
            base_query = base_query.filter(client_column == int(filter_value))
        except ValueError:
            pass

    month_start = date(year, month, 1)
    next_month = date(year + (1 if month == 12 else 0), 1 if month == 12 else month + 1, 1)

    priority_order = {
        "critical": 0,
        "high": 1,
        "medium": 2,
        "low": 3,
    }

    tickets = (
        base_query.filter(target_column.isnot(None))
        .filter(target_column >= month_start)
        .filter(target_column < next_month)
        .all()
    )

    def _sort_key(t):
        pval = t.priority.value if t.priority else ""
        created_ts = t.created_at.timestamp() if t.created_at else 0
        return (priority_order.get(pval, 99), -created_ts)

    tickets = sorted(tickets, key=_sort_key)

    events_by_date = defaultdict(list)
    for t in tickets:
        events_by_date[t.target_date].append(t)

    cal = calendar.Calendar(firstweekday=6)  # Sunday start
    weeks = cal.monthdatescalendar(year, month)

    prev_month_date = month_start - timedelta(days=1)
    prev_month_query = {"year": prev_month_date.year, "month": prev_month_date.month, "filter_by": filter_by, "filter_value": filter_value}
    next_month_date = next_month
    next_month_query = {"year": next_month_date.year, "month": next_month_date.month, "filter_by": filter_by, "filter_value": filter_value}
    if is_task_calendar:
        prev_month_query["scope"] = scope
        next_month_query["scope"] = scope

    return render_template(
        "tickets/calendar.html",
        weeks=weeks,
        month_start=month_start,
        today=today,
        events_by_date=events_by_date,
        engineers=engineers,
        clients=clients,
        selected_filter={
            "filter_by": filter_by,
            "filter_value": filter_value,
        },
        prev_month_query=prev_month_query,
        next_month_query=next_month_query,
        is_task_calendar=is_task_calendar,
        calendar_scope=scope,
    )


@tickets_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    is_client = current_user.role in CLIENT_SCOPED_ROLES

    if is_client:
        if not current_user.client_id:
            abort(403)
        clients = Client.query.filter(Client.id == current_user.client_id).all()
        instruments = Instrument.query.filter(Instrument.client_id == current_user.client_id).order_by(Instrument.name.asc()).all()
        apps = App.query.join(Client.apps).filter(Client.id == current_user.client_id).order_by(App.name.asc()).all()
    else:
        clients = Client.query.order_by(Client.name.asc()).all()
        instruments = Instrument.query.order_by(Instrument.name.asc()).all()
        apps = App.query.order_by(App.name.asc()).all()

    if request.method == "POST":
        created_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        client_id = current_user.client_id if is_client else request.form.get("client_id")
        ticket_for = request.form.get("ticket_for") or "instrument"
        instrument_id = request.form.get("instrument_id")
        app_id = request.form.get("app_id")
        subject = (request.form.get("subject") or "").strip()
        description = request.form.get("description")
        priority = (request.form.get("priority") or TicketPriority.NOT_SET.value).strip()
        target_date_raw = (request.form.get("target_date") or "").strip() if current_user.role == UserRole.ADMIN else ""
        date_needed_raw = (request.form.get("date_needed") or "").strip()
        photos = request.files.getlist("photos")

        instrument = Instrument.query.get(instrument_id) if instrument_id else None
        app_obj = App.query.get(app_id) if app_id else None
        
        target_date = None
        if target_date_raw:
            try:
                target_date = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid Target Schedule format.", "danger")
                return render_template(
                    "tickets/new.html",
                    clients=clients,
                    instruments=instruments,
                    apps=apps,
                    default_ticket_for="instrument" if current_user.role == UserRole.ENGINEER else "app"
                )

        date_needed = None
        if date_needed_raw:
            try:
                date_needed = datetime.strptime(date_needed_raw, "%Y-%m-%d")
            except ValueError:
                flash("Invalid Date Needed format.", "danger")
                return render_template(
                    "tickets/new.html",
                    clients=clients,
                    instruments=instruments,
                    apps=apps,
                    default_ticket_for="instrument" if current_user.role == UserRole.ENGINEER else "app"
                )

        if not client_id or not subject:
            flash("Client and complaint are required.", "danger")
            return render_template(
                "tickets/new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
            )

        if len(subject) > 100:
            flash("Complaint must be 100 characters or fewer.", "danger")
            return render_template(
                "tickets/new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
            )

        if ticket_for == "instrument" and not instrument_id:
            flash("Instrument is required for instrument tickets.", "danger")
            return render_template(
                "tickets/new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
            )

        if ticket_for == "app" and not app_id:
            flash("CDS Application is required for application tickets.", "danger")
            return render_template(
                "tickets/new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
            )

        if is_client:
            if ticket_for == "instrument" and (not instrument or instrument.client_id != current_user.client_id):
                flash("Invalid instrument selection for this client.", "danger")
                return render_template("tickets/new.html", clients=clients, instruments=instruments, apps=apps)

            if ticket_for == "app" and (not app_obj or current_user.client not in app_obj.clients):
                flash("Invalid application selection for this client.", "danger")
                return render_template("tickets/new.html", clients=clients, instruments=instruments, apps=apps)

        if ticket_for == "instrument" and not instrument:
            flash("Invalid instrument selection.", "danger")
            return render_template(
                "tickets/new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
            )

        if ticket_for == "instrument" and instrument and str(instrument.client_id) != str(client_id):
            flash("Selected instrument does not belong to the chosen client.", "danger")
            return render_template(
                "tickets/new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
            )

        ticket = Ticket(
            ticket_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
            client_id=client_id,
            instrument_id=instrument_id if ticket_for == "instrument" else None,
            app_id=app_id if ticket_for == "app" else None,
            ticket_for=ticket_for,
            reported_by_id=current_user.id,
            priority=TicketPriority(priority) if priority else TicketPriority.NOT_SET,
            subject=subject,
            description=description,
            status=TicketStatus.OPEN,
            created_at=created_at,
            updated_at=created_at,
            started_date=None,
            is_working=False,
            target_date=target_date,
            date_needed=date_needed,
        )

        db.session.add(ticket)
        db.session.flush()
        ticket.ticket_no = generate_ticket_no(ticket.id)

        upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
        os.makedirs(upload_folder, exist_ok=True)

        for photo in photos:
            if photo and photo.filename:
                safe_name = secure_filename(photo.filename)
                stored_name = f"{ticket.id}_{int(datetime.utcnow().timestamp() * 1000)}_{safe_name}"
                filepath = os.path.join(upload_folder, stored_name)
                photo.save(filepath)

                attachment = TicketAttachment(
                    ticket_id=ticket.id,
                    user_id=current_user.id,
                    stored_filename=stored_name,
                    original_filename=photo.filename,
                    content_type=photo.mimetype,
                    file_size=os.path.getsize(filepath),
                    uploaded_at=created_at,
                )
                db.session.add(attachment)

        ticket_creation_notifications = queue_ticket_creation_notifications(ticket, actor=current_user)
        db.session.commit()
        for recipient, notification in ticket_creation_notifications:
            emit_header_notification_added(recipient, notification)
        _emit_ticket_changed(ticket, "created")
        flash("Ticket created successfully.", "success")
        # return redirect(url_for("tickets.index"))
        return redirect(f"{url_for('tickets.detail', ticket_id=ticket.id)}#ticket-comments-card")
            
    default_ticket_for = "instrument" if current_user.role == UserRole.ENGINEER else "app"
    return render_template(
        "tickets/new.html",
        clients=clients,
        instruments=instruments,
        apps=apps,
        default_ticket_for=default_ticket_for
    )


@tickets_bp.route("/tasks/new", methods=["GET", "POST"])
@login_required
def create_task():
    can_create_tasks = _can_create_tasks(current_user)
    if not can_create_tasks:
        abort(403)

    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    apps = App.query.order_by(App.name.asc()).all()
    parent_tickets_query = _exclude_task_tickets(Ticket.query).filter(
        Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED])
    )
    if current_user.role in CLIENT_SCOPED_ROLES:
        parent_tickets_query = _apply_client_ticket_scope(parent_tickets_query)

    parent_tickets = parent_tickets_query.order_by(Ticket.created_at.desc(), Ticket.id.desc()).all()
    task_assignees = (
        User.query.filter(
            User.role == UserRole.ENGINEER,
            db.or_(*_task_assignee_filters(current_user)),
        )
        .order_by(User.full_name.asc(), User.username.asc())
        .all()
    )

    if request.method == "POST":
        parent_ticket_id_raw = (request.form.get("ticket_id") or "").strip()
        client_id_raw = (request.form.get("client_id") or "").strip()
        ticket_for = (request.form.get("ticket_for") or "instrument").strip()
        instrument_id_raw = (request.form.get("instrument_id") or "").strip()
        app_id_raw = (request.form.get("app_id") or "").strip()
        subject = (request.form.get("subject") or "").strip()
        description = (request.form.get("description") or "").strip()
        engineer_id_raw = (request.form.get("assigned_engineer_id") or "").strip()
        priority_raw = (request.form.get("priority") or "").strip()
        photos = request.files.getlist("photos")

        try:
            parent_ticket_id = int(parent_ticket_id_raw)
        except ValueError:
            parent_ticket_id = None

        try:
            client_id = int(client_id_raw) if client_id_raw else None
        except ValueError:
            client_id = None

        try:
            instrument_id = int(instrument_id_raw) if instrument_id_raw else None
        except ValueError:
            instrument_id = None

        try:
            app_id = int(app_id_raw) if app_id_raw else None
        except ValueError:
            app_id = None

        if current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
            engineer_id = current_user.id
        else:
            try:
                engineer_id = int(engineer_id_raw)
            except ValueError:
                engineer_id = None

        parent_ticket = Ticket.query.get(parent_ticket_id) if parent_ticket_id else None
        client = Client.query.get(client_id) if client_id else None
        instrument = Instrument.query.get(instrument_id) if instrument_id else None
        app_obj = App.query.get(app_id) if app_id else None
        engineer = None
        if engineer_id:
            engineer = User.query.filter(
                User.id == engineer_id,
                User.role == UserRole.ENGINEER,
                db.or_(*_task_assignee_filters(current_user)),
            ).first()

        if parent_ticket and _is_task_ticket(parent_ticket):
            flash("Select a valid parent ticket.", "danger")
            return render_template(
                "tickets/new_task.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
                parent_tickets=parent_tickets,
                task_assignees=task_assignees,
            )

        if parent_ticket and parent_ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            flash("Tasks cannot be created for completed or closed tickets.", "warning")
            return render_template(
                "tickets/new_task.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
                parent_tickets=parent_tickets,
                task_assignees=task_assignees,
            )

        if current_user.role in CLIENT_SCOPED_ROLES and parent_ticket:
            _ensure_client_ticket_access(parent_ticket)

        if not engineer:
            flash("Assign the task to a valid IT or Support user.", "danger")
            return render_template(
                "tickets/new_task.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
                parent_tickets=parent_tickets,
                task_assignees=task_assignees,
            )

        if parent_ticket:
            try:
                priority = TicketPriority(priority_raw) if priority_raw else (parent_ticket.priority or TicketPriority.NOT_SET)
            except ValueError:
                priority = parent_ticket.priority or TicketPriority.NOT_SET

            task_subject = subject or parent_ticket.subject
            task_description = (
                f"Task from ticket {parent_ticket.ticket_no}.\n\n"
                f"{description or parent_ticket.description or ''}"
            ).strip()
            task_client_id = parent_ticket.client_id
            task_instrument_id = parent_ticket.instrument_id if parent_ticket.ticket_for == "instrument" else None
            task_app_id = parent_ticket.app_id if parent_ticket.ticket_for == "app" else None
            task_ticket_for = parent_ticket.ticket_for
            task_ticket_id = parent_ticket.id
            task_target_date = parent_ticket.target_date
            task_date_needed = parent_ticket.date_needed
        else:
            if not client:
                flash("Client is required when no parent ticket is selected.", "danger")
                return render_template(
                    "tickets/new_task.html",
                    clients=clients,
                    instruments=instruments,
                    apps=apps,
                    parent_tickets=parent_tickets,
                    task_assignees=task_assignees,
                )

            if not subject:
                flash("Task is required when no parent ticket is selected.", "danger")
                return render_template(
                    "tickets/new_task.html",
                    clients=clients,
                    instruments=instruments,
                    apps=apps,
                    parent_tickets=parent_tickets,
                    task_assignees=task_assignees,
                )

            if ticket_for == "instrument" and instrument and instrument.client_id != client.id:
                flash("Selected instrument does not belong to the chosen client.", "danger")
                return render_template(
                    "tickets/new_task.html",
                    clients=clients,
                    instruments=instruments,
                    apps=apps,
                    parent_tickets=parent_tickets,
                    task_assignees=task_assignees,
                )

            if ticket_for == "app" and app_obj and client not in app_obj.clients:
                flash("Selected application is not available for the chosen client.", "danger")
                return render_template(
                    "tickets/new_task.html",
                    clients=clients,
                    instruments=instruments,
                    apps=apps,
                    parent_tickets=parent_tickets,
                    task_assignees=task_assignees,
                )

            try:
                priority = TicketPriority(priority_raw) if priority_raw else TicketPriority.NOT_SET
            except ValueError:
                priority = TicketPriority.NOT_SET

            task_subject = subject
            task_description = description
            task_client_id = client.id
            task_instrument_id = instrument.id if ticket_for == "instrument" and instrument else None
            task_app_id = app_obj.id if ticket_for == "app" and app_obj else None
            task_ticket_for = ticket_for
            task_ticket_id = None
            task_target_date = None
            task_date_needed = None

        created_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        task_ticket = TicketTask(
            task_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
            ticket_id=task_ticket_id,
            client_id=task_client_id,
            instrument_id=task_instrument_id,
            app_id=task_app_id,
            ticket_for=task_ticket_for,
            reported_by_id=current_user.id,
            assigned_engineer_id=engineer.id,
            assigned_by_id=current_user.id,
            priority=priority,
            subject=task_subject,
            description=task_description,
            status=TicketStatus.OPEN,
            started_date=None,
            is_working=False,
            target_date=task_target_date,
            date_needed=task_date_needed,
            created_at=created_at,
            updated_at=created_at,
        )
        db.session.add(task_ticket)
        db.session.flush()
        task_ticket.task_no = generate_task_ticket_no(task_ticket.id)
        if parent_ticket:
            _clone_ticket_attachments(parent_ticket, task_ticket, uploaded_at=task_ticket.created_at)

        upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
        if upload_folder:
            os.makedirs(upload_folder, exist_ok=True)

        for photo in photos:
            if photo and photo.filename:
                safe_name = secure_filename(photo.filename)
                stored_name = f"{task_ticket.id}_{int(datetime.utcnow().timestamp() * 1000)}_{safe_name}"
                filepath = os.path.join(upload_folder, stored_name)
                photo.save(filepath)

                attachment = TicketTaskAttachment(
                    ticket_task_id=task_ticket.id,
                    user_id=current_user.id,
                    stored_filename=stored_name,
                    original_filename=photo.filename,
                    content_type=photo.mimetype,
                    file_size=os.path.getsize(filepath),
                    uploaded_at=task_ticket.created_at,
                )
                db.session.add(attachment)
        if parent_ticket and _is_support_user(current_user):
            _sync_parent_ticket_priority_from_tasks(parent_ticket, current_user.full_name or current_user.username)
        db.session.commit()

        _emit_ticket_changed(task_ticket, "created")
        if parent_ticket:
            _emit_ticket_changed(parent_ticket, "updated")
        flash("Task created.", "success")
        return redirect(url_for("tickets.task_detail", task_id=task_ticket.id))

    return render_template(
        "tickets/new_task.html",
        clients=clients,
        instruments=instruments,
        apps=apps,
        parent_tickets=parent_tickets,
        task_assignees=task_assignees,
    )


@tickets_bp.route("/tasks")
@login_required
def developer_tasks():
    if not current_user.has_nav_access("developer_tasks"):
        abort(403)

    status_labels = {
        TicketStatus.OPEN: "Open",
        TicketStatus.IN_PROGRESS: "In-Process",
        TicketStatus.RESOLVED: "Fix/Completed",
        TicketStatus.REOPENED: "Re-Open",
        TicketStatus.CLOSED: "Closed",
        TicketStatus.ON_HOLD: "On Hold",
        TicketStatus.CANCELLED: "Cancelled",
    }

    clients = Client.query.order_by(Client.name.asc()).all()
    apps = App.query.order_by(App.name.asc()).all()
    reporters = User.query.filter(
        User.role == UserRole.ENGINEER,
        db.func.lower(User.user_type).in_(("it", "support")),
    ).order_by(User.full_name.asc(), User.username.asc()).all()
    assignees = User.query.filter(
        User.role == UserRole.ENGINEER,
        db.func.lower(User.user_type).in_(("it", "support")),
    ).order_by(User.full_name.asc(), User.username.asc()).all()

    query = TicketTask.query

    ticket_no_raw = (request.args.get("ticket_no") or "").strip()
    client_ids = [cid.strip() for cid in request.args.getlist("client_id") if cid.strip()]
    app_ids = [aid.strip() for aid in request.args.getlist("app_id") if aid.strip()]
    assignee_ids = [aid.strip() for aid in request.args.getlist("assignee_id") if aid.strip()]
    reported_by_ids = [rid.strip() for rid in request.args.getlist("reported_by_id") if rid.strip()]
    priority_values = [priority.strip() for priority in request.args.getlist("priority") if priority.strip()]
    status_values = [status.strip() for status in request.args.getlist("status") if status.strip()]
    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""

    if ticket_no_raw:
        query = query.filter(db.func.lower(TicketTask.task_no).like(f"%{ticket_no_raw.lower()}%"))

    if client_ids:
        try:
            query = query.filter(TicketTask.client_id.in_([int(cid) for cid in client_ids]))
        except ValueError:
            pass

    if app_ids:
        try:
            query = query.filter(TicketTask.app_id.in_([int(aid) for aid in app_ids]))
        except ValueError:
            pass

    if assignee_ids:
        try:
            query = query.filter(TicketTask.assigned_engineer_id.in_([int(aid) for aid in assignee_ids]))
        except ValueError:
            pass

    if reported_by_ids:
        try:
            query = query.filter(TicketTask.reported_by_id.in_([int(rid) for rid in reported_by_ids]))
        except ValueError:
            pass

    valid_priority_values = []
    for priority_raw in priority_values:
        try:
            valid_priority_values.append(TicketPriority(priority_raw))
        except ValueError:
            continue
    if valid_priority_values:
        query = query.filter(TicketTask.priority.in_(valid_priority_values))

    status_filter = _status_matches(TicketTask.status, status_values)
    if status_filter is not None:
        query = query.filter(status_filter)
    else:
        query = query.filter(~_status_matches(TicketTask.status, ["closed", "cancelled"]))

    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
            query = query.filter(TicketTask.created_at >= date_from)
        except ValueError:
            pass

    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d") + timedelta(days=1)
            query = query.filter(TicketTask.created_at < date_to)
        except ValueError:
            pass

    tasks = query.order_by(TicketTask.created_at.desc()).all()
    parent_ids = [task.ticket_id for task in tasks if task.ticket_id]
    task_states = {task.id: _task_work_session_state(task) for task in tasks}

    parents = {}
    if parent_ids:
        parents = {ticket.id: ticket for ticket in Ticket.query.filter(Ticket.id.in_(parent_ids)).all()}

    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return render_template(
            "tickets/_developer_tasks_table.html",
            tasks=tasks,
            parents=parents,
            status_labels=status_labels,
            task_states=task_states,
        )

    return render_template(
        "tickets/tasks.html",
        tasks=tasks,
        parents=parents,
        status_labels=status_labels,
        task_states=task_states,
        clients=clients,
        apps=apps,
        reporters=reporters,
        assignees=assignees,
        can_manage_developer_prompts=_can_manage_developer_prompts(current_user),
        current_date=datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d"),
        selected_filters={
            "ticket_no": ticket_no_raw,
            "client_ids": client_ids,
            "app_ids": app_ids,
            "assignee_ids": assignee_ids,
            "reported_by_ids": reported_by_ids,
            "priority": priority_values,
            "status": [status.strip().lower() for status in status_values if status.strip()],
            "date_from": date_from_raw,
            "date_to": date_to_raw,
        },
    )


@tickets_bp.route("/tasks/workload")
@login_required
def developer_workload():
    if not current_user.has_nav_access("developer_workload"):
        abort(403)

    status_labels = {
        TicketStatus.OPEN: "Open",
        TicketStatus.IN_PROGRESS: "In-Process",
        TicketStatus.RESOLVED: "Fix/Completed",
        TicketStatus.REOPENED: "Re-Open",
        TicketStatus.CLOSED: "Closed",
        TicketStatus.ON_HOLD: "On Hold",
        TicketStatus.CANCELLED: "Cancelled",
    }
    now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    today_start = datetime.combine(now.date(), time.min)
    today = now.date()
    default_date_from = today - timedelta(days=29)
    date_from_raw = (request.args.get("date_from") or "").strip()
    date_to_raw = (request.args.get("date_to") or "").strip()
    view_mode = (request.args.get("view") or "card").strip().lower()
    if view_mode not in {"card", "list"}:
        view_mode = "card"

    date_from_value = date_from_raw
    date_to_value = date_to_raw

    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date()
        except ValueError:
            date_from = default_date_from
            date_from_value = default_date_from.isoformat()
    else:
        date_from = default_date_from
        date_from_value = default_date_from.isoformat()

    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date()
        except ValueError:
            date_to = today
            date_to_value = today.isoformat()
    else:
        date_to = today
        date_to_value = today.isoformat()

    if date_from > date_to:
        date_from, date_to = date_to, date_from
        date_from_value = date_from.isoformat()
        date_to_value = date_to.isoformat()

    period_start = datetime.combine(date_from, time.min)
    period_end = min(datetime.combine(date_to + timedelta(days=1), time.min), now)
    period_label = f"{date_from_value} to {date_to_value}"
    can_view_all = current_user.has_nav_access("developer_workload")

    developer_query = User.query.filter(User.role == UserRole.ENGINEER)
    if not can_view_all:
        developer_query = developer_query.filter(User.id == current_user.id)
    developers = developer_query.order_by(User.user_type.asc(), User.full_name.asc(), User.username.asc()).all()
    developer_ids = [developer.id for developer in developers]

    tasks = []
    sessions = []
    if developer_ids:
        tasks = TicketTask.query.filter(TicketTask.assigned_engineer_id.in_(developer_ids)).all()
        sessions = TicketTaskWorkSession.query.filter(TicketTaskWorkSession.developer_id.in_(developer_ids)).all()

    tasks_by_developer = defaultdict(list)
    for task in tasks:
        tasks_by_developer[task.assigned_engineer_id].append(task)

    sessions_by_developer = defaultdict(list)
    for session in sessions:
        sessions_by_developer[session.developer_id].append(session)

    def _seconds_in_window(session, window_start, window_end=None):
        started_at = session.started_at
        if not started_at:
            return 0
        ended_at = session.ended_at or session.paused_at or now
        effective_start = max(started_at, window_start or started_at)
        effective_end = min(ended_at, window_end or now, now)
        if effective_end <= effective_start:
            return 0
        return int((effective_end - effective_start).total_seconds())

    def _seconds_in_period(session):
        return _seconds_in_window(session, period_start, period_end)

    def _seconds_today(session):
        return _seconds_in_window(session, today_start)

    def _format_duration(seconds):
        hours = seconds / 3600
        if hours >= 10:
            return f"{hours:.0f}h"
        if hours >= 1:
            return f"{hours:.1f}h"
        minutes = int(round(seconds / 60))
        return f"{minutes}m"

    def _completed_at(task):
        return task.closed_at or task.updated_at or task.created_at

    rows = []
    for developer in developers:
        developer_tasks = tasks_by_developer.get(developer.id, [])
        developer_sessions = sessions_by_developer.get(developer.id, [])
        active_tasks = [
            task for task in developer_tasks
            if task.status not in (TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED)
        ]
        completed_tasks = [
            task for task in developer_tasks
            if task.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
            and _completed_at(task)
            and _completed_at(task) >= period_start
            and _completed_at(task) < period_end
        ]
        completed_today_tasks = [
            task for task in developer_tasks
            if task.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED)
            and _completed_at(task)
            and _completed_at(task) >= today_start
        ]
        working_tasks = [task for task in developer_tasks if task.is_working]
        paused_tasks = [
            task for task in active_tasks
            if not task.is_working and _task_work_session_state(task) == "paused"
        ]
        overdue_tasks = [
            task for task in active_tasks
            if task.target_date and task.target_date < today
        ]
        due_today_tasks = [
            task for task in active_tasks
            if task.target_date == today
        ]
        high_priority_tasks = [
            task for task in active_tasks
            if task.priority == TicketPriority.HIGH
        ]
        critical_priority_tasks = [
            task for task in active_tasks
            if task.priority == TicketPriority.CRITICAL
        ]
        open_count = sum(1 for task in developer_tasks if task.status in (TicketStatus.OPEN, TicketStatus.REOPENED))
        in_progress_count = sum(1 for task in developer_tasks if task.status == TicketStatus.IN_PROGRESS)
        resolved_count = sum(1 for task in developer_tasks if task.status == TicketStatus.RESOLVED)
        total_work_seconds = sum(_seconds_in_period(session) for session in developer_sessions)
        today_work_seconds = sum(_seconds_today(session) for session in developer_sessions)
        completed_with_work = [
            task for task in completed_tasks
            if any(session.ticket_task_id == task.id for session in developer_sessions)
        ]
        avg_work_seconds = 0
        if completed_with_work:
            seconds_by_task = defaultdict(int)
            for session in developer_sessions:
                seconds_by_task[session.ticket_task_id] += _seconds_in_period(session)
            avg_work_seconds = int(sum(seconds_by_task[task.id] for task in completed_with_work) / len(completed_with_work))
        denominator = len(active_tasks) + len(completed_tasks)
        completion_rate = round((len(completed_tasks) / denominator) * 100) if denominator else 0
        workload_score = len(active_tasks)
        recent_tasks = sorted(
            active_tasks,
            key=lambda task: (
                0 if task.is_working else 1 if _task_work_session_state(task) == "paused" else 2,
                task.target_date or date.max,
                task.created_at or datetime.min,
            ),
        )
        completed_recent_tasks = sorted(
            completed_tasks,
            key=lambda task: (
                0 if task.status == TicketStatus.RESOLVED else 1,
                -(_completed_at(task).timestamp() if _completed_at(task) else 0),
                -(task.id or 0),
            ),
        )
        display_tasks = recent_tasks + completed_recent_tasks
        recent_task_states = {
            task.id: _task_work_session_state(task)
            for task in display_tasks
        }

        rows.append(
            {
                "developer": developer,
                "active": len(active_tasks),
                "working": len(working_tasks),
                "paused": len(paused_tasks),
                "open": open_count,
                "in_progress": in_progress_count,
                "resolved": resolved_count,
                "completed": len(completed_tasks),
                "completed_today": len(completed_today_tasks),
                "overdue": len(overdue_tasks),
                "due_today": len(due_today_tasks),
                "high_priority": len(high_priority_tasks),
                "critical_priority": len(critical_priority_tasks),
                "total_work_seconds": total_work_seconds,
                "total_work_label": _format_duration(total_work_seconds),
                "today_work_seconds": today_work_seconds,
                "today_work_label": _format_duration(today_work_seconds),
                "avg_work_label": _format_duration(avg_work_seconds) if avg_work_seconds else "N/A",
                "completion_rate": completion_rate,
                "workload_score": workload_score,
                "recent_tasks": recent_tasks,
                "display_tasks": display_tasks,
                "recent_task_states": recent_task_states,
            }
        )

    rows.sort(key=lambda row: (-row["workload_score"], -row["working"], -row["overdue"], row["developer"].full_name or row["developer"].username))
    totals = {
        "active": sum(row["active"] for row in rows),
        "working": sum(row["working"] for row in rows),
        "paused": sum(row["paused"] for row in rows),
        "completed": sum(row["completed"] for row in rows),
        "completed_today": sum(row["completed_today"] for row in rows),
        "overdue": sum(row["overdue"] for row in rows),
        "work_label": _format_duration(sum(row["total_work_seconds"] for row in rows)),
        "today_work_label": _format_duration(sum(row["today_work_seconds"] for row in rows)),
    }

    return render_template(
        "tickets/developer_workload.html",
        rows=rows,
        totals=totals,
        status_labels=status_labels,
        TicketStatus=TicketStatus,
        date_from_value=date_from_value,
        date_to_value=date_to_value,
        period_label=period_label,
        view_mode=view_mode,
        can_view_all=can_view_all,
        now=now,
    )


@tickets_bp.route("/<int:ticket_id>", methods=["GET", "POST"])
@login_required
def detail(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    if _is_task_ticket(ticket):
        migrated_task = TicketTask.query.filter_by(task_no=ticket.ticket_no).first()
        if migrated_task:
            return redirect(url_for("tickets.task_detail", task_id=migrated_task.id))

    _ensure_client_ticket_access(ticket)
    if current_user.role in READ_ONLY_ROLES and _is_task_ticket(ticket):
        abort(403)
    if request.method == "POST":
        closed_redirect = _ensure_ticket_not_closed(ticket)
        if closed_redirect:
            return closed_redirect
    else:
        did_mark_notifications = False
        recipients_to_refresh = []
        if mark_ticket_notifications_read_for_user(ticket.id, current_user):
            did_mark_notifications = True
            recipients_to_refresh.append(current_user.id)
        if current_user.role == UserRole.ADMIN or _is_support_user(current_user):
            shared_recipient_ids = mark_shared_ticket_comment_notifications_read(ticket.id)
            if shared_recipient_ids:
                did_mark_notifications = True
                recipients_to_refresh.extend(shared_recipient_ids)
        if did_mark_notifications:
            db.session.commit()
            for recipient_id in sorted(set(recipients_to_refresh)):
                recipient = User.query.get(recipient_id)
                if recipient:
                    emit_header_notification_snapshot(recipient)

    is_task_ticket = _is_task_ticket(ticket)
    parent_ticket = None
    if is_task_ticket:
        try:
            parent_ticket_id = int((ticket.category or "").replace(TASK_CATEGORY_PREFIX, "", 1))
            parent_ticket = Ticket.query.get(parent_ticket_id)
        except ValueError:
            parent_ticket = None

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    comments_raw = sorted(
        ticket.comments,
        key=lambda c: (to_localtime(c.created_at).replace(tzinfo=None) if c.created_at else datetime.min, c.id or 0),
    )
    if current_user.role in READ_ONLY_ROLES:
        comments_raw = [c for c in comments_raw if not c.is_internal]

    comments = [c for c in comments_raw if not _is_change_comment(c.comment_text)]

    engineers = []
    task_engineers = []
    if current_user.role in (UserRole.ADMIN, UserRole.ENGINEER):
        if ticket.ticket_for == "app":
            engineers = User.query.filter(
                User.user_type == "IT",
                User.role == UserRole.ENGINEER
            ).order_by(User.full_name.asc()).all()
        elif ticket.ticket_for == "instrument":
            engineers = User.query.filter(
                User.user_type == "Engineer",
                User.role == UserRole.ENGINEER
            ).order_by(User.full_name.asc()).all()
        else:
            engineers = User.query.filter(
                User.role == UserRole.ENGINEER
            ).order_by(User.full_name.asc()).all()
        task_engineers_query = User.query.filter(
            User.role == UserRole.ENGINEER,
            db.func.lower(User.user_type).in_(("it", "support")),
        )
        task_engineers = task_engineers_query.order_by(User.full_name.asc(), User.username.asc()).all()

    ticket_tasks = TicketTask.query.filter(TicketTask.ticket_id == ticket.id).order_by(
        TicketTask.created_at.desc(),
        TicketTask.id.desc()
    ).all()

    timeline_events = []
    timeline_event_seq = 0

    def add_event(timestamp, title, description, kind="update", storage="guess"):
        nonlocal timeline_event_seq
        if not timestamp:
            return
        timeline_event_seq += 1
        timeline_events.append(
            {
                "timestamp": timestamp,
                "display_timestamp": _timeline_timestamp(timestamp, storage=storage),
                "title": title,
                "description": description,
                "type": kind,
                "sort_seq": timeline_event_seq,
            }
        )

    def _is_initial_attachment(att: TicketAttachment) -> bool:
        if not ticket.created_at or not att.uploaded_at:
            return False
        return abs((att.uploaded_at - ticket.created_at).total_seconds()) <= 5

    def _is_comment_attachment(att: TicketAttachment, comment: TicketComment) -> bool:
        if not att.uploaded_at or not comment.created_at:
            return False
        if att.user_id != comment.user_id:
            return False
        return abs((att.uploaded_at - comment.created_at).total_seconds()) <= 10

    if request.method == "POST":
        comment_text = (request.form.get("comment_text") or "").strip()
        parent_comment_id = request.form.get("parent_comment_id", type=int)
        is_internal = False if current_user.role in READ_ONLY_ROLES else bool(request.form.get("is_internal"))
        files = request.files.getlist("attachment")
        has_uploaded_files = any(file and file.filename for file in files)
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        did_something = False
        new_comment = None
        added_attachments = []
        propagated_child_tasks = []
        parent_comment = None

        if parent_comment_id:
            parent_comment = TicketComment.query.filter_by(
                id=parent_comment_id,
                ticket_id=ticket.id,
            ).first()
            if not parent_comment or parent_comment.deleted or _is_change_comment(parent_comment.comment_text):
                if wants_json:
                    return jsonify({"ok": False, "message": "Reply target was not found."}), 400
                flash("Reply target was not found.", "warning")
                return redirect(f"{url_for('tickets.detail', ticket_id=ticket.id)}#ticket-comments-card")

        if comment_text or has_uploaded_files:
            comment = TicketComment(
                ticket_id=ticket.id,
                parent_comment_id=parent_comment.id if parent_comment else None,
                user_id=current_user.id,
                comment_text=comment_text,
                is_internal=is_internal,
            )
            db.session.add(comment)
            new_comment = comment
            did_something = True

        upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
        if upload_folder:
            os.makedirs(upload_folder, exist_ok=True)

        for file in files:
            if file and file.filename:
                safe_name = secure_filename(file.filename)
                stored_name = f"{ticket.id}_{int(datetime.utcnow().timestamp() * 1000)}_{safe_name}"
                filepath = os.path.join(upload_folder, stored_name)
                file.save(filepath)

                att = TicketAttachment(
                    ticket_id=ticket.id,
                    user_id=current_user.id,
                    stored_filename=stored_name,
                    original_filename=file.filename,
                    content_type=file.mimetype,
                    file_size=os.path.getsize(filepath),
                )
                db.session.add(att)
                added_attachments.append(att)
                did_something = True

        if (
            added_attachments
            and current_user.role in CLIENT_SCOPED_ROLES
            and not _is_task_ticket(ticket)
        ):
            child_tasks = TicketTask.query.filter(TicketTask.ticket_id == ticket.id).all()
            cloned_uploaded_at = new_comment.created_at if new_comment and new_comment.created_at else None
            for child_task in child_tasks:
                _clone_attachment_records(added_attachments, child_task, uploaded_at=cloned_uploaded_at)
                propagated_child_tasks.append(child_task)

        if did_something:
            db.session.commit()
            if new_comment:
                _emit_ticket_comment(ticket, new_comment, attachments=added_attachments)
                for child_task in propagated_child_tasks:
                    _emit_ticket_changed(child_task, "updated")
                if wants_json:
                    return jsonify({"ok": True, "comment": _ticket_comment_payload(new_comment, attachments=added_attachments)})
            else:
                _emit_ticket_changed(ticket, "updated")
                for child_task in propagated_child_tasks:
                    _emit_ticket_changed(child_task, "updated")
                if wants_json:
                    return jsonify({"ok": True, "reload": True})
            flash("Update saved.", "success")
        else:
            if wants_json:
                return jsonify({"ok": False, "message": "Nothing to save."}), 400
            flash("Nothing to save.", "warning")

        return redirect(f"{url_for('tickets.detail', ticket_id=ticket.id)}#ticket-comments-card")

    reported_by_name = ticket.reported_by.full_name or ticket.reported_by.username
    add_event(ticket.created_at, "Ticket created", f"Reported by {reported_by_name}.", "created", storage="local")

    for c in comments_raw:
        if not _is_change_comment(c.comment_text):
            continue
        if _hide_change_comment_for_viewer(c.comment_text):
            continue
        commenter = c.user.full_name or c.user.username
        add_event(
            c.created_at,
            f"Updated by {commenter}",
            c.comment_text,
            "update",
            storage="utc",
        )

    for att in sorted(
        ticket.attachments,
        key=lambda att: (to_localtime(att.uploaded_at).replace(tzinfo=None) if att.uploaded_at else datetime.min, att.id or 0),
    ):
        if _is_initial_attachment(att):
            continue
        sender = att.user.full_name or att.user.username if att.user else "Unknown"
        add_event(
            att.uploaded_at,
            f"Attachment added by {sender}",
            att.original_filename,
            "attachment",
            storage="utc",
        )

    for log in ticket.service_logs:
        visit_dt = None
        if log.visit_date:
            visit_dt = datetime.combine(log.visit_date, log.start_time or time(12, 0))
            visit_dt_storage = "local"
        else:
            visit_dt = log.created_at
            visit_dt_storage = "utc"

        engineer_name = log.engineer.full_name or log.engineer.username
        log_type = (log.service_type or "Service").title()
        description = f"{engineer_name} recorded a {log_type.lower()} visit."
        add_event(visit_dt, f"Service log ({log_type})", description, "log", storage=visit_dt_storage)

    if ticket.status == TicketStatus.CLOSED:
        add_event(ticket.closed_at, "Ticket closed", "Marked as closed.", "closed", storage="local")
    elif ticket.status == TicketStatus.RESOLVED:
        add_event(ticket.updated_at, "Ticket resolved", "Awaiting confirmation.", "resolved", storage="local")

    timeline_events.sort(
        key=lambda e: (
            e["display_timestamp"].replace(tzinfo=None) if e.get("display_timestamp") else datetime.min,
            e.get("sort_seq", 0),
        ),
    )

    comment_attachments = {}
    assigned_attachment_ids = set()
    non_initial_attachments = [att for att in ticket.attachments if not _is_initial_attachment(att)]
    for comment in comments:
        if comment.deleted:
            comment_attachments[comment.id] = []
            continue
        matched = []
        for att in non_initial_attachments:
            if att.id in assigned_attachment_ids:
                continue
            if _is_comment_attachment(att, comment):
                matched.append(att)
                assigned_attachment_ids.add(att.id)
        comment_attachments[comment.id] = matched

    remaining_attachments = [att for att in ticket.attachments if att.id not in assigned_attachment_ids]

    status_options = [
        (TicketStatus.OPEN, "Open"),
        (TicketStatus.IN_PROGRESS, "In-Process"),
        (TicketStatus.RESOLVED, "Fix/Completed"),
        (TicketStatus.REOPENED, "Re-Open"),
        (TicketStatus.CLOSED, "Closed"),
        (TicketStatus.CANCELLED, "Cancelled"),
    ]
    status_labels = {k: v for k, v in status_options}
    priority_options = [
        (TicketPriority.NOT_SET, "Not Set"),
        (TicketPriority.CRITICAL, "Critical"),
        (TicketPriority.HIGH, "High"),
        (TicketPriority.MEDIUM, "Medium"),
        (TicketPriority.LOW, "Low"),
    ]
    support_can_edit_core_fields = _can_fully_edit_task_detail(current_user)
    editable_clients = Client.query.order_by(Client.name.asc()).all() if support_can_edit_core_fields else []
    editable_apps = App.query.order_by(App.name.asc()).all() if support_can_edit_core_fields else []
    comment_templates = _comment_templates_for_user(current_user)
    
    return render_template(
        "tickets/detail.html",
        ticket=ticket,
        comments=comments,
        reaction_options=REACTION_OPTIONS,
        status_options=status_options,
        status_labels=status_labels,
        priority_options=priority_options,
        engineers=engineers,
        task_engineers=task_engineers,
        ticket_tasks=ticket_tasks,
        is_task_ticket=is_task_ticket,
        parent_ticket=parent_ticket,
        task_work_session_state=None,
        TicketStatus=TicketStatus,
        default_target_date=today_str,
        ticket_created_display_at=local_naive_to_localtime(ticket.created_at),
        timeline_events=timeline_events,
        comment_attachments=comment_attachments,
        remaining_attachments=remaining_attachments,
        editable_clients=editable_clients,
        editable_apps=editable_apps,
        support_can_edit_core_fields=support_can_edit_core_fields,
        comment_templates=comment_templates,
    )


@tickets_bp.route("/tasks/<int:task_id>", methods=["GET", "POST"])
@login_required
def task_detail(task_id):
    task = TicketTask.query.get_or_404(task_id)
    parent_ticket = task.parent_ticket
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if not _can_view_task_detail(task):
        abort(403)
    if request.method != "POST" and mark_task_notifications_read_for_user(task.id, current_user):
        db.session.commit()
        emit_header_notification_snapshot(current_user)

    if request.method == "POST":
        closed_redirect = _ensure_task_not_closed(task)
        if closed_redirect:
            return closed_redirect

        comment_text = (request.form.get("comment_text") or "").strip()
        parent_comment_id = request.form.get("parent_comment_id", type=int)
        is_internal = bool(request.form.get("is_internal"))
        files = request.files.getlist("attachment")
        has_uploaded_files = any(file and file.filename for file in files)
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        did_something = False
        new_comment = None
        added_attachments = []
        parent_comment = None

        if parent_comment_id:
            parent_comment = TicketTaskComment.query.filter_by(
                id=parent_comment_id,
                ticket_task_id=task.id,
            ).first()
            if not parent_comment or parent_comment.deleted or _is_change_comment(parent_comment.comment_text):
                if wants_json:
                    return jsonify({"ok": False, "message": "Reply target was not found."}), 400
                flash("Reply target was not found.", "warning")
                return redirect(f"{url_for('tickets.task_detail', task_id=task.id)}#ticket-comments-card")

        if comment_text or has_uploaded_files:
            new_comment = TicketTaskComment(
                ticket_task_id=task.id,
                parent_comment_id=parent_comment.id if parent_comment else None,
                user_id=current_user.id,
                comment_text=comment_text,
                is_internal=is_internal,
            )
            db.session.add(new_comment)
            did_something = True

        upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
        if upload_folder:
            os.makedirs(upload_folder, exist_ok=True)

        for file in files:
            if file and file.filename:
                safe_name = secure_filename(file.filename)
                stored_name = f"task_{task.id}_{int(datetime.utcnow().timestamp() * 1000)}_{safe_name}"
                filepath = os.path.join(upload_folder, stored_name)
                file.save(filepath)
                att = TicketTaskAttachment(
                    ticket_task_id=task.id,
                    user_id=current_user.id,
                    stored_filename=stored_name,
                    original_filename=file.filename,
                    content_type=file.mimetype,
                    file_size=os.path.getsize(filepath),
                )
                db.session.add(att)
                added_attachments.append(att)
                did_something = True

        if did_something:
            task.updated_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
            task_comment_notifications = []
            if new_comment and not new_comment.is_internal:
                task_comment_notifications = queue_task_comment_notifications(task, actor=current_user)
            db.session.commit()
            for recipient, notification in task_comment_notifications:
                emit_header_notification_added(recipient, notification)
            _emit_ticket_changed(task, "commented" if new_comment else "updated")
            if wants_json and new_comment:
                return jsonify({"ok": True, "comment": _task_comment_payload(new_comment, attachments=added_attachments)})
            if wants_json:
                return jsonify({"ok": True, "reload": True})
            flash("Update saved.", "success")
        else:
            if wants_json:
                return jsonify({"ok": False, "message": "Nothing to save."}), 400
            flash("Nothing to save.", "warning")

        return redirect(f"{url_for('tickets.task_detail', task_id=task.id)}#ticket-comments-card")

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    comments_raw = sorted(
        task.comments,
        key=lambda c: (to_localtime(c.created_at).replace(tzinfo=None) if c.created_at else datetime.min, c.id or 0),
    )
    comments = [c for c in comments_raw if not _is_change_comment(c.comment_text)]

    task_engineers = User.query.filter(
        User.role == UserRole.ENGINEER,
        db.func.lower(User.user_type).in_(("it", "support")),
    ).order_by(User.full_name.asc(), User.username.asc()).all()
    engineers = task_engineers

    timeline_events = []
    timeline_event_seq = 0

    def add_event(timestamp, title, description, kind="update", storage="guess"):
        nonlocal timeline_event_seq
        if not timestamp:
            return
        timeline_event_seq += 1
        timeline_events.append(
            {
                "timestamp": timestamp,
                "display_timestamp": _timeline_timestamp(timestamp, storage=storage),
                "title": title,
                "description": description,
                "type": kind,
                "sort_seq": timeline_event_seq,
            }
        )

    def _is_initial_attachment(att: TicketTaskAttachment) -> bool:
        if not task.created_at or not att.uploaded_at:
            return False
        return abs((att.uploaded_at - task.created_at).total_seconds()) <= 5

    def _is_comment_attachment(att: TicketTaskAttachment, comment: TicketTaskComment) -> bool:
        if not att.uploaded_at or not comment.created_at:
            return False
        if att.user_id != comment.user_id:
            return False
        return abs((att.uploaded_at - comment.created_at).total_seconds()) <= 10

    reported_by_name = task.reported_by.full_name or task.reported_by.username
    add_event(task.created_at, "Task created", f"Created by {reported_by_name}.", "created", storage="local")

    for c in comments_raw:
        if not _is_change_comment(c.comment_text):
            continue
        commenter = c.user.full_name or c.user.username
        add_event(c.created_at, f"Updated by {commenter}", c.comment_text, "update", storage="utc")

    for att in sorted(
        task.attachments,
        key=lambda att: (to_localtime(att.uploaded_at).replace(tzinfo=None) if att.uploaded_at else datetime.min, att.id or 0),
    ):
        if _is_initial_attachment(att):
            continue
        sender = att.user.full_name or att.user.username if att.user else "Unknown"
        add_event(att.uploaded_at, f"Attachment added by {sender}", att.original_filename, "attachment", storage="utc")

    if task.status == TicketStatus.CLOSED:
        add_event(task.closed_at, "Task closed", "Marked as closed.", "closed", storage="local")
    elif task.status == TicketStatus.RESOLVED:
        add_event(task.updated_at, "Task resolved", "Awaiting confirmation.", "resolved", storage="local")

    timeline_events.sort(
        key=lambda e: (
            e["display_timestamp"].replace(tzinfo=None) if e.get("display_timestamp") else datetime.min,
            e.get("sort_seq", 0),
        ),
    )

    comment_attachments = {}
    assigned_attachment_ids = set()
    non_initial_attachments = [att for att in task.attachments if not _is_initial_attachment(att)]
    for comment in comments:
        if comment.deleted:
            comment_attachments[comment.id] = []
            continue
        matched = []
        for att in non_initial_attachments:
            if att.id in assigned_attachment_ids:
                continue
            if _is_comment_attachment(att, comment):
                matched.append(att)
                assigned_attachment_ids.add(att.id)
        comment_attachments[comment.id] = matched

    remaining_attachments = [att for att in task.attachments if att.id not in assigned_attachment_ids]
    status_options = [
        (TicketStatus.OPEN, "Open"),
        (TicketStatus.IN_PROGRESS, "In-Process"),
        (TicketStatus.RESOLVED, "Fix/Completed"),
        (TicketStatus.REOPENED, "Re-Open"),
        (TicketStatus.CLOSED, "Closed"),
        (TicketStatus.CANCELLED, "Cancelled"),
    ]
    priority_options = [
        (TicketPriority.NOT_SET, "Not Set"),
        (TicketPriority.CRITICAL, "Critical"),
        (TicketPriority.HIGH, "High"),
        (TicketPriority.MEDIUM, "Medium"),
        (TicketPriority.LOW, "Low"),
    ]
    support_can_edit_core_fields = _can_fully_edit_task_detail(current_user)
    editable_clients = Client.query.order_by(Client.name.asc()).all() if support_can_edit_core_fields else []
    editable_apps = App.query.order_by(App.name.asc()).all() if support_can_edit_core_fields else []
    comment_templates = _comment_templates_for_user(current_user)

    return render_template(
        "tickets/detail.html",
        ticket=task,
        comments=comments,
        reaction_options=REACTION_OPTIONS,
        status_options=status_options,
        status_labels={k: v for k, v in status_options},
        priority_options=priority_options,
        engineers=engineers,
        task_engineers=task_engineers,
        ticket_tasks=[],
        is_task_ticket=True,
        parent_ticket=parent_ticket,
        task_work_session_state=_task_work_session_state(task),
        TicketStatus=TicketStatus,
        default_target_date=today_str,
        ticket_created_display_at=local_naive_to_localtime(task.created_at),
        ticket_updated_display_at=local_naive_to_localtime(task.updated_at),
        ticket_closed_display_at=local_naive_to_localtime(task.closed_at),
        timeline_events=timeline_events,
        comment_attachments=comment_attachments,
        remaining_attachments=remaining_attachments,
        editable_clients=editable_clients,
        editable_apps=editable_apps,
        support_can_edit_core_fields=support_can_edit_core_fields,
        comment_templates=comment_templates,
    )


@tickets_bp.route("/comments/<int:comment_id>/edit", methods=["POST"])
@login_required
def edit_comment(comment_id):
    comment = TicketComment.query.get_or_404(comment_id)
    ticket = comment.ticket

    _ensure_client_ticket_access(ticket)
    if ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return jsonify({"error": "Closed or cancelled tickets are read-only."}), 403
    if not _can_modify_ticket_comment(comment):
        return jsonify({"error": "You can only edit your own active comments."}), 403

    comment_text = (request.form.get("comment_text") or "").strip()
    if not comment_text:
        return jsonify({"error": "Comment text is required."}), 400

    comment.comment_text = comment_text
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    return jsonify({"ok": True, "comment": _ticket_comment_payload(comment)})


@tickets_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    comment = TicketComment.query.get_or_404(comment_id)
    ticket = comment.ticket

    _ensure_client_ticket_access(ticket)
    if ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return jsonify({"error": "Closed or cancelled tickets are read-only."}), 403
    if not _can_modify_ticket_comment(comment):
        return jsonify({"error": "You can only delete your own active comments."}), 403

    comment.deleted = True
    comment.deleted_at = datetime.utcnow()
    comment.reactions_json = None
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    return jsonify({"ok": True, "comment": _ticket_comment_payload(comment)})


@tickets_bp.route("/task-comments/<int:comment_id>/edit", methods=["POST"])
@login_required
def edit_task_comment(comment_id):
    comment = TicketTaskComment.query.get_or_404(comment_id)
    task = comment.task
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if task.status == TicketStatus.CLOSED:
        return jsonify({"error": "Closed tasks are read-only."}), 403
    if not _can_modify_task_comment(comment):
        return jsonify({"error": "You can only edit your own active comments."}), 403

    comment_text = (request.form.get("comment_text") or "").strip()
    if not comment_text:
        return jsonify({"error": "Comment text is required."}), 400

    comment.comment_text = comment_text
    task.updated_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    db.session.commit()
    _emit_ticket_changed(task, "updated")
    return jsonify({"ok": True, "comment": _task_comment_payload(comment)})


@tickets_bp.route("/task-comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_task_comment(comment_id):
    comment = TicketTaskComment.query.get_or_404(comment_id)
    task = comment.task
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if task.status == TicketStatus.CLOSED:
        return jsonify({"error": "Closed tasks are read-only."}), 403
    if not _can_modify_task_comment(comment):
        return jsonify({"error": "You can only delete your own active comments."}), 403

    comment.deleted = True
    comment.deleted_at = datetime.utcnow()
    comment.reactions_json = None
    task.updated_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    db.session.commit()
    _emit_ticket_changed(task, "updated")
    return jsonify({"ok": True, "comment": _task_comment_payload(comment)})


@tickets_bp.route("/comments/<int:comment_id>/react", methods=["POST"])
@login_required
def react_comment(comment_id):
    comment = TicketComment.query.get_or_404(comment_id)
    ticket = comment.ticket

    _ensure_client_ticket_access(ticket)
    if ticket.status in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return jsonify({"error": "Closed or cancelled tickets are read-only."}), 403
    if current_user.role in READ_ONLY_ROLES and _is_task_ticket(ticket):
        abort(403)
    if comment.is_internal and current_user.role in READ_ONLY_ROLES:
        abort(403)
    if comment.deleted:
        return jsonify({"error": "Deleted comments cannot be reacted to."}), 400

    emoji = (request.form.get("emoji") or "").strip()
    allowed_emojis = {"👍", "❤️", "😂", "😮", "😢", "😡"}
    allowed_reactions = {item["code"] for item in REACTION_OPTIONS} | {"acknowledge"}
    if emoji and emoji not in allowed_reactions:
        return jsonify({"error": "Invalid reaction."}), 400

    reaction_state_map = comment.reaction_state_map()
    user_key = str(current_user.id)

    current_state = reaction_state_map.get(user_key, {"reaction": "", "acknowledge": False})
    reaction_code = current_state.get("reaction", "")
    acknowledge = bool(current_state.get("acknowledge"))

    if not emoji:
        reaction_state_map.pop(user_key, None)
    elif emoji == "acknowledge":
        acknowledge = not acknowledge
    else:
        reaction_code = "" if reaction_code == emoji else emoji

    if reaction_code or acknowledge:
        reaction_state_map[user_key] = {
            "reaction": reaction_code,
            "acknowledge": acknowledge,
        }
    else:
        reaction_state_map.pop(user_key, None)

    comment.reactions_json = json.dumps(reaction_state_map, ensure_ascii=False) if reaction_state_map else None
    db.session.commit()
    _emit_comment_reaction(comment)
    return jsonify(_comment_reaction_payload(comment))


@tickets_bp.route("/task-comments/<int:comment_id>/react", methods=["POST"])
@login_required
def react_task_comment(comment_id):
    comment = TicketTaskComment.query.get_or_404(comment_id)
    task = comment.task
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if task.status == TicketStatus.CLOSED:
        return jsonify({"error": "Closed tasks are read-only."}), 403
    if comment.is_internal and current_user.role in READ_ONLY_ROLES:
        abort(403)
    if comment.deleted:
        return jsonify({"error": "Deleted comments cannot be reacted to."}), 400

    emoji = (request.form.get("emoji") or "").strip()
    allowed_reactions = {item["code"] for item in REACTION_OPTIONS} | {"acknowledge"}
    if emoji and emoji not in allowed_reactions:
        return jsonify({"error": "Invalid reaction."}), 400

    reaction_state_map = comment.reaction_state_map()
    user_key = str(current_user.id)
    state = reaction_state_map.get(user_key, {"reaction": "", "acknowledge": False})
    if emoji == "acknowledge":
        state["acknowledge"] = not bool(state.get("acknowledge"))
    else:
        state["reaction"] = "" if state.get("reaction") == emoji else emoji

    if state.get("reaction") or state.get("acknowledge"):
        reaction_state_map[user_key] = state
    else:
        reaction_state_map.pop(user_key, None)

    comment.reactions_json = json.dumps(reaction_state_map, ensure_ascii=False) if reaction_state_map else None
    db.session.commit()
    socketio.emit(
        "ticket_comment_reacted",
        {
            "ticket_id": comment.ticket_task_id,
            "comment_id": comment.id,
            "reactions": comment.reaction_summary(),
        },
        room=f"ticket:{task.id}",
    )
    if task.ticket_id:
        socketio.emit(
            "ticket_comment_reacted",
            {
                "ticket_id": comment.ticket_task_id,
                "comment_id": comment.id,
                "reactions": comment.reaction_summary(),
            },
            room=f"ticket:{task.ticket_id}",
        )
    return jsonify(_task_comment_reaction_payload(comment))


@tickets_bp.route("/<int:ticket_id>/tasks", methods=["POST"])
@login_required
def create_tasks(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect
    can_create_tasks = _can_create_tasks(current_user)
    if not can_create_tasks:
        abort(403)
    if _is_task_ticket(ticket):
        flash("Tasks cannot be created from another task.", "warning")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))
    if ticket.status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
        flash("Tasks cannot be created for completed or closed tickets.", "warning")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    subjects = request.form.getlist("task_subjects[]")
    descriptions = request.form.getlist("task_descriptions[]")
    engineer_ids = request.form.getlist("task_engineer_ids[]")
    priorities = request.form.getlist("task_priorities[]")

    task_payloads = []
    max_rows = max(len(subjects), len(descriptions), len(engineer_ids), len(priorities), 0)
    for idx in range(max_rows):
        subject = (subjects[idx] if idx < len(subjects) else "").strip()
        description = (descriptions[idx] if idx < len(descriptions) else "").strip()
        engineer_id = (engineer_ids[idx] if idx < len(engineer_ids) else "").strip()
        priority_raw = (priorities[idx] if idx < len(priorities) else "").strip()

        if not subject and not engineer_id and not description:
            continue
        if not engineer_id:
            flash("Each task must be assigned to a valid engineer.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        try:
            engineer_id_int = int(engineer_id)
        except ValueError:
            flash("Invalid task assignment selected.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        engineer_query = User.query.filter(
            User.id == engineer_id_int,
            User.role == UserRole.ENGINEER,
        )
        engineer_query = engineer_query.filter(db.or_(*_task_assignee_filters(current_user)))
        engineer = engineer_query.first()
        if not engineer:
            flash("Invalid task assignment selected.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        try:
            priority = TicketPriority(priority_raw) if priority_raw else (ticket.priority or TicketPriority.NOT_SET)
        except ValueError:
            priority = ticket.priority or TicketPriority.NOT_SET

        task_payloads.append(
            {
                "subject": subject or ticket.subject,
                "description": description,
                "engineer": engineer,
                "priority": priority,
            }
        )

    if not task_payloads:
        flash("Add at least one task.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    created_task_tickets = []
    for payload in task_payloads:
        created_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        task_ticket = TicketTask(
            task_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
            ticket_id=ticket.id,
            client_id=ticket.client_id,
            instrument_id=ticket.instrument_id if ticket.ticket_for == "instrument" else None,
            app_id=ticket.app_id if ticket.ticket_for == "app" else None,
            ticket_for=ticket.ticket_for,
            reported_by_id=current_user.id,
            assigned_engineer_id=payload["engineer"].id,
            assigned_by_id=current_user.id,
            priority=payload["priority"],
            subject=payload["subject"],
            description=(
                f"Task from ticket {ticket.ticket_no}.\n\n"
                f"{payload['description'] or ticket.description or ''}"
            ).strip(),
            status=TicketStatus.OPEN,
            started_date=None,
            is_working=False,
            target_date=ticket.target_date,
            date_needed=ticket.date_needed,
            created_at=created_at,
            updated_at=created_at,
        )
        db.session.add(task_ticket)
        db.session.flush()
        task_ticket.task_no = generate_task_ticket_no(task_ticket.id)
        _clone_ticket_attachments(ticket, task_ticket, uploaded_at=task_ticket.created_at)
        created_task_tickets.append(task_ticket)

    if _is_support_user(current_user):
        _sync_parent_ticket_priority_from_tasks(ticket, current_user.full_name or current_user.username)
    db.session.commit()
    for task_ticket in created_task_tickets:
        _emit_ticket_changed(task_ticket, "created")
    _emit_ticket_changed(ticket, "updated")
    flash(f"{len(task_payloads)} task(s) created.", "success")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/status", methods=["POST"])
@login_required
def update_status(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    new_status_raw = request.form.get("status")
    valid_statuses = {
        TicketStatus.OPEN.value: TicketStatus.OPEN,
        TicketStatus.IN_PROGRESS.value: TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED.value: TicketStatus.RESOLVED,
        TicketStatus.REOPENED.value: TicketStatus.REOPENED,
        TicketStatus.CLOSED.value: TicketStatus.CLOSED,
        TicketStatus.CANCELLED.value: TicketStatus.CANCELLED,
    }

    new_status = valid_statuses.get(new_status_raw)
    if not new_status:
        flash("Invalid status selection.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    if (
        new_status == TicketStatus.CLOSED
        and ticket.reported_by
        and ticket.reported_by.role in CLIENT_SCOPED_ROLES
    ):
        if not ticket.signature_attachment:
            signature_data = request.form.get("signature_data") or ""
            if not signature_data:
                flash("Client signature is required before closing tickets reported by a client.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            try:
                header, encoded = signature_data.split(",", 1) if "," in signature_data else ("", signature_data)
                sig_bytes = base64.b64decode(encoded)
            except Exception:
                flash("Invalid signature data. Please recapture the signature.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
            if not upload_folder:
                flash("Signature upload folder is not configured.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            os.makedirs(upload_folder, exist_ok=True)
            filename = f"{ticket.id}_signature_{int(datetime.utcnow().timestamp())}.png"
            filepath = os.path.join(upload_folder, filename)
            with open(filepath, "wb") as f:
                f.write(sig_bytes)
            attachment = TicketAttachment(
                ticket_id=ticket.id,
                user_id=current_user.id,
                stored_filename=filename,
                original_filename="signature.png",
                content_type="image/png",
                file_size=len(sig_bytes),
            )
            db.session.add(attachment)

    actor_name = current_user.full_name or current_user.username
    _update_ticket_status_value(ticket, new_status, actor_name)
    closed_child_tasks = []
    if new_status == TicketStatus.CLOSED:
        closed_child_tasks = _close_child_tasks(ticket, actor_name)
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    for child_task in closed_child_tasks:
        _emit_ticket_changed(child_task, "updated")
    flash("Ticket status updated.", "success")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/kanban-status", methods=["POST"])
@login_required
def update_kanban_status(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return jsonify({"error": "Closed or cancelled tickets are read-only."}), 403

    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    column_key = (request.form.get("column") or "").strip().lower()
    if not column_key:
        column_key = (request.form.get("status") or "").strip().lower()

    if column_key not in {"backlog", "open", "in_progress", "resolved", "currently_working"}:
        return jsonify({"error": "Invalid kanban status."}), 400

    actor_name = current_user.full_name or current_user.username
    changed = False
    completion_notifications = []

    if column_key == "backlog":
        if ticket.kanban_bucket != "backlog":
            ticket.kanban_bucket = "backlog"
            changed = True
        if ticket.is_working:
            ticket.is_working = False
            changed = True
    elif column_key == "open":
        changed = _update_ticket_status_value(ticket, TicketStatus.OPEN, actor_name)
        if ticket.is_working:
            ticket.is_working = False
            changed = True
    elif column_key == "in_progress":
        changed = _update_ticket_status_value(ticket, TicketStatus.IN_PROGRESS, actor_name)
        if ticket.kanban_bucket:
            ticket.kanban_bucket = None
            changed = True
        if not ticket.is_working:
            ticket.is_working = True
            changed = True
        if not ticket.started_date:
            ticket.started_date = datetime.utcnow().date()
            changed = True
    elif column_key == "resolved":
        changed = _update_ticket_status_value(ticket, TicketStatus.RESOLVED, actor_name)
        if ticket.kanban_bucket:
            ticket.kanban_bucket = None
            changed = True
        if ticket.is_working:
            ticket.is_working = False
            changed = True

    if changed:
        db.session.commit()
        _emit_ticket_changed(ticket, "updated")

    return jsonify(
        {
            "ok": True,
            "changed": changed,
            "ticket_id": ticket.id,
            "status": ticket.status.value if ticket.status else "",
        }
    )


@tickets_bp.route("/tasks/<int:task_id>/kanban-status", methods=["POST"])
@login_required
def update_task_kanban_status(task_id):
    task = TicketTask.query.get_or_404(task_id)
    closed_redirect = _ensure_task_not_closed(task)
    if closed_redirect:
        return jsonify({"error": "Closed tasks are read-only."}), 403

    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if current_user.role == UserRole.ENGINEER and not _is_support_user(current_user) and task.assigned_engineer_id != current_user.id:
        abort(403)

    column_key = (request.form.get("column") or "").strip().lower()
    if not column_key:
        column_key = (request.form.get("status") or "").strip().lower()

    if column_key not in {"backlog", "open", "in_progress", "resolved", "currently_working"}:
        return jsonify({"error": "Invalid kanban status."}), 400

    actor_name = current_user.full_name or current_user.username
    changed = False

    if column_key == "backlog":
        if task.kanban_bucket != "backlog":
            task.kanban_bucket = "backlog"
            changed = True
        if task.is_working:
            task.is_working = False
            changed = True
    elif column_key == "open":
        changed = _update_task_status_value(task, TicketStatus.OPEN, actor_name)
        if task.is_working:
            task.is_working = False
            changed = True
    elif column_key == "in_progress":
        changed = _update_task_status_value(task, TicketStatus.IN_PROGRESS, actor_name)
        if task.kanban_bucket:
            task.kanban_bucket = None
            changed = True
        if not task.is_working:
            task.is_working = True
            changed = True
        if not task.started_date:
            task.started_date = datetime.utcnow().date()
            changed = True
    elif column_key == "resolved":
        changed = _update_task_status_value(task, TicketStatus.RESOLVED, actor_name)
        if task.kanban_bucket:
            task.kanban_bucket = None
            changed = True
        if task.is_working:
            task.is_working = False
            changed = True
        if changed:
            completion_notifications = queue_task_completed_notifications(task, actor=current_user)

    if changed:
        db.session.commit()
        for recipient, notification in completion_notifications:
            emit_header_notification_added(recipient, notification)
        _emit_ticket_changed(task, "updated")

    return jsonify(
        {
            "ok": True,
            "changed": changed,
            "ticket_id": task.id,
            "status": task.status.value if task.status else "",
        }
    )


@tickets_bp.route("/<int:ticket_id>/work_state", methods=["POST"])
@login_required
def toggle_work_state(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect

    is_it_user = (current_user.user_type or "").lower() == "it"
    if ticket.assigned_engineer_id != current_user.id or not is_it_user:
        abort(403)

    action = (request.form.get("action") or "").lower()
    if action not in ("start", "stop"):
        flash("Invalid work action.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    name = current_user.full_name or current_user.username
    parent_ticket = None
    if _is_task_ticket(ticket):
        try:
            parent_ticket_id = int((ticket.category or "").replace(TASK_CATEGORY_PREFIX, "", 1))
            parent_ticket = Ticket.query.get(parent_ticket_id)
        except ValueError:
            parent_ticket = None

    if action == "start":
        ticket.is_working = True
        ticket.started_date = datetime.utcnow().date()
        if ticket.status in (TicketStatus.OPEN, TicketStatus.REOPENED, TicketStatus.ON_HOLD):
            ticket.status = TicketStatus.IN_PROGRESS
        message_status = "Working"
        if parent_ticket and parent_ticket.status in (TicketStatus.OPEN, TicketStatus.REOPENED, TicketStatus.ON_HOLD):
            parent_ticket.status = TicketStatus.IN_PROGRESS
    else:
        ticket.is_working = False
        message_status = "Not Working"

    _record_ticket_change(ticket, f"Work status changed to {message_status} by {name}.")
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    if parent_ticket:
        _emit_ticket_changed(parent_ticket, "updated")
    flash("Ticket work status updated.", "success")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/priority", methods=["POST"])
@login_required
def update_priority(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    new_priority_raw = request.form.get("priority")
    valid_priorities = {
        TicketPriority.NOT_SET.value: TicketPriority.NOT_SET,
        TicketPriority.CRITICAL.value: TicketPriority.CRITICAL,
        TicketPriority.HIGH.value: TicketPriority.HIGH,
        TicketPriority.MEDIUM.value: TicketPriority.MEDIUM,
        TicketPriority.LOW.value: TicketPriority.LOW,
    }

    new_priority = valid_priorities.get(new_priority_raw)
    if not new_priority:
        flash("Invalid priority selection.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    old_priority = ticket.priority
    ticket.priority = new_priority
    if new_priority != old_priority:
        _record_ticket_change(
            ticket,
            f"Priority changed from {_enum_label(old_priority)} to {_enum_label(new_priority)} by {current_user.full_name or current_user.username}.",
        )
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    flash("Ticket priority updated.", "success")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/target_date", methods=["POST"])
@login_required
def update_target_date(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    new_target_date_raw = request.form.get("target_date")
    valid_priorities = {
        TicketPriority.NOT_SET.value: TicketPriority.NOT_SET,
        TicketPriority.CRITICAL.value: TicketPriority.CRITICAL,
        TicketPriority.HIGH.value: TicketPriority.HIGH,
        TicketPriority.MEDIUM.value: TicketPriority.MEDIUM,
        TicketPriority.LOW.value: TicketPriority.LOW,
    }

    if not new_target_date_raw:
        flash("Invalid target date selection.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    try:
        new_target_date = datetime.strptime(new_target_date_raw, "%Y-%m-%d").date()
    except ValueError:
        flash("Invalid target date format.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    old_target = ticket.target_date
    ticket.target_date = new_target_date
    if old_target != new_target_date:
        _record_ticket_change(
            ticket,
            f"Target schedule changed from {old_target.strftime('%Y-%m-%d') if old_target else 'Not set'} to {new_target_date.strftime('%Y-%m-%d')} by {current_user.full_name or current_user.username}.",
        )
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    flash("Ticket target date updated.", "success")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/resolution", methods=["POST"])
@login_required
def resolve_decision(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect
    action = request.form.get("resolution_action")

    if current_user.role == UserRole.SALES:
        abort(403)

    if ticket.status != TicketStatus.RESOLVED:
        flash("Resolution actions are only available when ticket is Fix/Completed.", "warning")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    if action == "accept":
        # ticket.status = TicketStatus.CLOSED
        # ticket.closed_at = datetime.utcnow()
        new_status = TicketStatus.CLOSED       
        old_status = ticket.status
        
        if not ticket.signature_attachment:
            signature_data = request.form.get("signature_data") or ""
            if not signature_data:
                flash("Client signature is required before closing tickets reported by a client.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            try:
                header, encoded = signature_data.split(",", 1) if "," in signature_data else ("", signature_data)
                sig_bytes = base64.b64decode(encoded)
            except Exception:
                flash("Invalid signature data. Please recapture the signature.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
            if not upload_folder:
                flash("Signature upload folder is not configured.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            os.makedirs(upload_folder, exist_ok=True)
            filename = f"{ticket.id}_signature_{int(datetime.utcnow().timestamp())}.png"
            filepath = os.path.join(upload_folder, filename)
            with open(filepath, "wb") as f:
                f.write(sig_bytes)
            attachment = TicketAttachment(
                ticket_id=ticket.id,
                user_id=current_user.id,
                stored_filename=filename,
                original_filename="signature.png",
                content_type="image/png",
                file_size=len(sig_bytes),
            )
            db.session.add(attachment)

        ticket.status = new_status

        _record_ticket_change(
            ticket,
            f"Status changed from {_enum_label(old_status)} to {_enum_label(new_status)} by {current_user.full_name or current_user.username}.",
        )
        closed_child_tasks = _close_child_tasks(ticket, current_user.full_name or current_user.username)

        db.session.commit()
        _emit_ticket_changed(ticket, "updated")
        for child_task in closed_child_tasks:
            _emit_ticket_changed(child_task, "updated")
        flash("Ticket accepted and closed.", "success")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    if action == "deny":
        closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        old_ticket_tasks = TicketTask.query.filter(TicketTask.ticket_id == ticket.id).order_by(
            TicketTask.created_at.asc(),
            TicketTask.id.asc(),
        ).all()

        # Close current ticket
        ticket.status = TicketStatus.CLOSED
        ticket.closed_at = closed_at

        # Create new ticket referencing the previous one
        new_ticket = Ticket(
            ticket_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
            client_id=ticket.client_id,
            instrument_id=ticket.instrument_id,
            app_id=ticket.app_id,
            ticket_for=ticket.ticket_for,
            reported_by_id=ticket.reported_by_id,
            assigned_engineer_id=ticket.assigned_engineer_id,
            assigned_by_id=ticket.assigned_by_id,
            priority=ticket.priority,
            subject=ticket.subject,
            description=f"Previous Ticket No: {ticket.ticket_no}\n\n{ticket.description or ''}",
            status=TicketStatus.OPEN,
            started_date=None,
            is_working=False,
            target_date=ticket.target_date,
            date_needed=ticket.date_needed,
        )
        db.session.add(new_ticket)
        db.session.flush()
        new_ticket.ticket_no = generate_ticket_no(new_ticket.id)
        _clone_ticket_attachments(ticket, new_ticket, uploaded_at=new_ticket.created_at)

        recreated_tasks = []
        for old_task in old_ticket_tasks:
            old_task.status = TicketStatus.CLOSED
            old_task.closed_at = closed_at
            old_task.is_working = False
            old_task.kanban_bucket = None

            created_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
            new_task = TicketTask(
                task_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
                ticket_id=new_ticket.id,
                client_id=old_task.client_id,
                instrument_id=old_task.instrument_id,
                app_id=old_task.app_id,
                ticket_for=old_task.ticket_for,
                reported_by_id=old_task.reported_by_id,
                assigned_engineer_id=old_task.assigned_engineer_id,
                assigned_by_id=old_task.assigned_by_id,
                priority=old_task.priority,
                subject=old_task.subject,
                description=f"Previous Task Ticket No: {old_task.ticket_no}\n\n{old_task.description or ''}",
                status=TicketStatus.OPEN,
                started_date=None,
                is_working=False,
                target_date=old_task.target_date,
                date_needed=old_task.date_needed,
                created_at=created_at,
                updated_at=created_at,
            )
            db.session.add(new_task)
            db.session.flush()
            new_task.task_no = generate_task_ticket_no(new_task.id)
            _clone_ticket_attachments(old_task, new_task, uploaded_at=new_task.created_at)
            recreated_tasks.append((old_task, new_task))

        db.session.commit()
        _emit_ticket_changed(ticket, "updated")
        _emit_ticket_changed(new_ticket, "created")
        for old_task, new_task in recreated_tasks:
            _emit_ticket_changed(old_task, "updated")
            _emit_ticket_changed(new_task, "created")
        flash(f"Ticket denied and new ticket {new_ticket.ticket_no} created.", "success")
        return redirect(url_for("tickets.detail", ticket_id=new_ticket.id))

    flash("Invalid resolution action.", "danger")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/prompt-client-resolution", methods=["POST"])
@login_required
def prompt_client_resolution(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)

    if _is_task_ticket(ticket):
        abort(404)
    if not _can_prompt_client_resolution(current_user):
        abort(403)
    if ticket.status != TicketStatus.RESOLVED:
        flash("Client prompt is only available when the ticket is Fix/Completed.", "warning")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    recipient_count = _emit_client_resolution_prompt(ticket, sender=current_user)
    if recipient_count:
        flash("Client prompt sent.", "success")
    else:
        flash("No active client account found for this ticket.", "warning")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/prompt-client-resolution-global", methods=["POST"])
@login_required
def prompt_client_resolution_global():
    if not _can_prompt_client_resolution(current_user):
        abort(403)

    client_ids = []
    for client_id_raw in request.form.getlist("client_id"):
        client_id_raw = (client_id_raw or "").strip()
        if not client_id_raw:
            continue
        try:
            client_ids.append(int(client_id_raw))
        except ValueError:
            continue

    recipient_count = _emit_global_client_resolution_prompts(
        sender=current_user,
        client_ids=client_ids or None,
    )
    if recipient_count:
        flash("Client prompts sent.", "success")
    else:
        flash("No Fix/Completed tickets are waiting for client acceptance.", "warning")
    return redirect(url_for("tickets.index"))


@tickets_bp.route("/developer-prompts", methods=["GET"])
@login_required
def developer_prompt_history():
    if not _can_manage_developer_prompts(current_user):
        abort(403)

    prompts = (
        DeveloperPrompt.query
        .filter(DeveloperPrompt.created_by_id == current_user.id)
        .order_by(DeveloperPrompt.created_at.desc(), DeveloperPrompt.id.desc())
        .all()
    )

    prompt_rows = []
    for prompt in prompts:
        counts = prompt.response_counts()
        responses = sorted(
            prompt.responses or [],
            key=lambda row: (
                0 if (row.response_status or "pending") == "pending" else 1,
                (row.user.full_name or row.user.username or "").lower() if row.user else "",
            ),
        )
        prompt_rows.append(
            {
                "prompt": prompt,
                "counts": counts,
                "responses": responses,
            }
        )

    return render_template("tickets/developer_prompts.html", prompt_rows=prompt_rows)


@tickets_bp.route("/developer-prompts", methods=["POST"])
@login_required
def create_developer_prompt():
    if not _can_manage_developer_prompts(current_user):
        abort(403)

    title = (request.form.get("title") or "").strip()
    message = (request.form.get("message") or "").strip()
    if not message:
        flash("Prompt message is required.", "warning")
        return redirect(url_for("tickets.developer_tasks"))

    prompt, recipient_count = _create_developer_prompt(current_user, title, message)
    if not prompt or not recipient_count:
        flash("No active developer accounts found for this prompt.", "warning")
        return redirect(url_for("tickets.developer_tasks"))

    db.session.commit()
    _emit_developer_prompt(prompt)
    flash(f"Developer prompt sent to {recipient_count} user{'s' if recipient_count != 1 else ''}.", "success")
    return redirect(url_for("tickets.developer_prompt_history"))


@tickets_bp.route("/developer-prompts/pending")
@login_required
def developer_prompt_state():
    return jsonify(_pending_developer_prompt_state_for_user(current_user))


@tickets_bp.route("/developer-prompts/<int:prompt_id>/respond", methods=["POST"])
@login_required
def respond_developer_prompt(prompt_id):
    response_row = (
        DeveloperPromptResponse.query
        .filter(
            DeveloperPromptResponse.prompt_id == prompt_id,
            DeveloperPromptResponse.user_id == current_user.id,
        )
        .first_or_404()
    )

    action = (request.form.get("action") or "").strip().lower()
    if request.is_json and not action:
        payload = request.get_json(silent=True) or {}
        action = (payload.get("action") or "").strip().lower()
    if action not in {"confirm", "deny"}:
        return jsonify({"ok": False, "error": "Invalid prompt response."}), 400
    if (response_row.response_status or "pending") != "pending":
        return jsonify({"ok": False, "error": "This prompt already has a recorded response."}), 409

    response_row.response_status = "confirmed" if action == "confirm" else "denied"
    response_row.responded_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    db.session.commit()

    creator = response_row.prompt.created_by if response_row.prompt else None
    if creator:
        counts = response_row.prompt.response_counts()
        socketio.emit(
            "developer_prompt_response_updated",
            {
                "prompt_id": response_row.prompt_id,
                "user_id": current_user.id,
                "user": current_user.full_name or current_user.username or "User",
                "response_status": response_row.response_status,
                "responded_at": to_localtime(response_row.responded_at).strftime("%Y-%m-%d %H:%M") if response_row.responded_at else "",
                "counts": counts,
            },
            room=f"user_notifications:{creator.id}",
        )

    next_state = _pending_developer_prompt_state_for_user(current_user)
    return jsonify(
        {
            "ok": True,
            "count": next_state.get("count", 0),
            "prompt": next_state.get("prompt"),
            "response_status": response_row.response_status,
        }
    )


@tickets_bp.route("/<int:ticket_id>/assign", methods=["POST"])
@login_required
def update_assignee(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    def _eng_label(eng_obj):
        return eng_obj.full_name or eng_obj.username if eng_obj else "None"

    engineer_id_raw = request.form.get("engineer_id")
    if not engineer_id_raw:
        previous = ticket.assigned_engineer
        ticket.assigned_engineer_id = None
        ticket.assigned_by_id = current_user.id
        if previous:
            _record_ticket_change(
                ticket,
                f"Engineer/IT assignment cleared (previously {_eng_label(previous)}) by {current_user.full_name or current_user.username}.",
            )
        db.session.commit()
        _emit_ticket_changed(ticket, "updated")
        flash("Assigned engineer cleared.", "success")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    engineer = User.query.filter(User.id == engineer_id_raw, User.role == UserRole.ENGINEER).first()
    if not engineer:
        flash("Invalid engineer selection.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    previous = ticket.assigned_engineer
    ticket.assigned_engineer_id = engineer.id
    ticket.assigned_by_id = current_user.id
    if ticket.status == TicketStatus.OPEN:
        ticket.status = TicketStatus.IN_PROGRESS
    if engineer != previous:
        _record_ticket_change(
            ticket,
            f"Engineer/IT assigned to {_eng_label(engineer)} (previously {_eng_label(previous)}) by {current_user.full_name or current_user.username}.",
        )
    db.session.commit()
    _emit_ticket_changed(ticket, "updated")
    flash("Engineer assigned.", "success")
    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/export")
@login_required
def export():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "Ticket No",
        "Client",
        "Instrument",
        "Complain",
        "Priority",
        "Status",
        "Reported By",
        "Created At",
    ])

    status_labels = {
        TicketStatus.OPEN: "Open",
        TicketStatus.IN_PROGRESS: "In-Process",
        TicketStatus.RESOLVED: "Fix/Completed",
        TicketStatus.REOPENED: "Re-Open",
        TicketStatus.CLOSED: "Closed",
        TicketStatus.ON_HOLD: "On Hold",
        TicketStatus.CANCELLED: "Cancelled",
    }

    if current_user.role in CLIENT_SCOPED_ROLES:
        tickets = _apply_client_ticket_scope(_exclude_task_tickets(Ticket.query)).order_by(Ticket.created_at.desc()).all()
    elif current_user.role == UserRole.SALES:
        tickets = _exclude_task_tickets(Ticket.query).order_by(Ticket.created_at.desc()).all()
    else:
        tickets = Ticket.query.order_by(Ticket.created_at.desc()).all()

    for t in tickets:
        writer.writerow([
            t.ticket_no,
            t.client.name if t.client else "",
            t.instrument.display_label() if t.instrument else "",
            t.subject,
            t.priority.value if t.priority else "",
            status_labels.get(t.status, t.status.value if t.status else ""),
            format_reported_by_name(t, current_user),
            t.created_at.strftime("%Y-%m-%d %H:%M") if t.created_at else "",
        ])

    resp = Response(output.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=tickets_export.csv"
    return resp


@tickets_bp.route("/tasks/<int:task_id>/update-info", methods=["POST"])
@login_required
def update_task_info(task_id):
    task = TicketTask.query.get_or_404(task_id)
    closed_redirect = _ensure_task_not_closed(task)
    if closed_redirect:
        return closed_redirect
    # if current_user.role in READ_ONLY_ROLES:
    #     abort(403)
    
    new_status_raw = (request.form.get("status") or "").strip()
    
    is_task_owner = current_user.id in [
        task.reported_by_id,
        task.assigned_engineer_id,
        task.assigned_by_id,
    ]

    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    if (
        new_status_raw == TicketStatus.CANCELLED.value
        and not (
            current_user.role == UserRole.ADMIN
            or _is_support_user(current_user)
            or is_task_owner
        )
    ):
        abort(403)

    changed = False
    user_name = current_user.full_name or current_user.username
    # support_can_edit_core_fields = _can_edit_core_ticket_fields(current_user)
    support_can_edit_core_fields = _can_fully_edit_task_detail(current_user)
    
    new_target_date_raw = (request.form.get("target_date") or "").strip()
    old_target_date = task.target_date
    new_target_date = None
    if new_target_date_raw:
        try:
            new_target_date = datetime.strptime(new_target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid target date format.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))

    completion_notifications = []

    if old_target_date != new_target_date:
        task.target_date = new_target_date
        _record_task_change(
            task,
            f"Target schedule changed from {old_target_date.strftime('%Y-%m-%d') if old_target_date else 'Not set'} to {new_target_date.strftime('%Y-%m-%d') if new_target_date else 'Not set'} by {user_name}."
        )
        changed = True

    new_priority_raw = (request.form.get("priority") or "").strip()
    valid_priorities = {
        TicketPriority.NOT_SET.value: TicketPriority.NOT_SET,
        TicketPriority.CRITICAL.value: TicketPriority.CRITICAL,
        TicketPriority.HIGH.value: TicketPriority.HIGH,
        TicketPriority.MEDIUM.value: TicketPriority.MEDIUM,
        TicketPriority.LOW.value: TicketPriority.LOW,
    }
    new_priority = valid_priorities.get(new_priority_raw)
    if new_priority and task.priority != new_priority:
        old_priority = task.priority
        task.priority = new_priority
        _record_task_change(task, f"Priority changed from {_enum_label(old_priority)} to {_enum_label(new_priority)} by {user_name}.")
        changed = True

    valid_statuses = {
        TicketStatus.OPEN.value: TicketStatus.OPEN,
        TicketStatus.IN_PROGRESS.value: TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED.value: TicketStatus.RESOLVED,
        TicketStatus.REOPENED.value: TicketStatus.REOPENED,
        TicketStatus.CLOSED.value: TicketStatus.CLOSED,
        TicketStatus.CANCELLED.value: TicketStatus.CANCELLED,
    }
    # new_status = valid_statuses.get((request.form.get("status") or "").strip())
    new_status = valid_statuses.get(new_status_raw)
    if not new_status:
        flash("Invalid status selection.", "danger")
        return redirect(url_for("tickets.task_detail", task_id=task.id))
    if task.status != new_status:
        old_status = task.status
        task.status = new_status
        task.kanban_bucket = None
        if new_status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            task.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        elif new_status == TicketStatus.REOPENED:
            task.closed_at = None
        _record_task_change(task, f"Status changed from {_enum_label(old_status)} to {_enum_label(new_status)} by {user_name}.")
        changed = True
        if new_status == TicketStatus.RESOLVED:
            completion_notifications = queue_task_completed_notifications(task, actor=current_user)

    if "engineer_id" in request.form:
        if not _can_reassign_task():
            abort(403)

        engineer_id_raw = (request.form.get("engineer_id") or "").strip()
        old_engineer = task.assigned_engineer
        new_engineer = None
        if engineer_id_raw:
            new_engineer = User.query.filter(User.id == engineer_id_raw, User.role == UserRole.ENGINEER).first()
            if not new_engineer:
                flash("Invalid engineer selection.", "danger")
                return redirect(url_for("tickets.task_detail", task_id=task.id))
        if (old_engineer.id if old_engineer else None) != (new_engineer.id if new_engineer else None):
            task.assigned_engineer_id = new_engineer.id if new_engineer else None
            task.assigned_by_id = current_user.id
            _record_task_change(
                task,
                f"Engineer/IT assigned to {new_engineer.full_name or new_engineer.username if new_engineer else 'None'} (previously {old_engineer.full_name or old_engineer.username if old_engineer else 'None'}) by {user_name}."
            )
            changed = True

    if any(key in request.form for key in ("subject", "description", "client_id", "app_id")):
        if not support_can_edit_core_fields:
            abort(403)

        subject = (request.form.get("subject") or "").strip()
        description = (request.form.get("description") or "").strip()
        client_id_raw = (request.form.get("client_id") or "").strip()
        app_id_raw = (request.form.get("app_id") or "").strip()

        if not subject:
            flash("Title is required.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))
        if len(subject) > 100:
            flash("Title must be 100 characters or fewer.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))
        if not client_id_raw:
            flash("Client is required.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))
        try:
            client_id = int(client_id_raw)
        except ValueError:
            flash("Invalid client selection.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))
        client = Client.query.get(client_id)
        if not client:
            flash("Invalid client selection.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))

        normalized_description = description or None
        if task.subject != subject or (task.description or None) != normalized_description:
            task.subject = subject
            task.description = normalized_description
            _record_task_change(task, f"Title/description updated by {user_name}.")
            changed = True

        if task.client_id != client.id:
            previous_client = task.client.name if task.client else "None"
            task.client_id = client.id
            if task.instrument_id and task.instrument and task.instrument.client_id != client.id:
                task.instrument_id = None
            if task.app_id and task.app and client not in task.app.clients:
                task.app_id = None
            _record_task_change(task, f"Client changed from {previous_client} to {client.name} by {user_name}.")
            changed = True

        if app_id_raw:
            try:
                app_id = int(app_id_raw)
            except ValueError:
                flash("Invalid application selection.", "danger")
                return redirect(url_for("tickets.task_detail", task_id=task.id))
            app_obj = App.query.get(app_id)
            if not app_obj:
                flash("Invalid application selection.", "danger")
                return redirect(url_for("tickets.task_detail", task_id=task.id))
            if client not in app_obj.clients:
                flash("Selected application does not belong to the selected client.", "danger")
                return redirect(url_for("tickets.task_detail", task_id=task.id))

            if task.app_id != app_obj.id or task.ticket_for != "app":
                previous_app = task.app.name if task.app else "None"
                task.app_id = app_obj.id
                task.ticket_for = "app"
                task.instrument_id = None
                _record_task_change(task, f"Application changed from {previous_app} to {app_obj.name} by {user_name}.")
                changed = True

    if changed and task.parent_ticket and _is_support_user(current_user):
        changed = _sync_parent_ticket_priority_from_tasks(task.parent_ticket, user_name) or changed

    if changed:
        db.session.commit()
        for recipient, notification in completion_notifications:
            emit_header_notification_added(recipient, notification)
        _emit_ticket_changed(task, "updated")
        if task.parent_ticket and _is_support_user(current_user):
            _emit_ticket_changed(task.parent_ticket, "updated")
        flash("Task info updated successfully.", "success")
    else:
        flash("No changes detected.", "warning")

    return redirect(url_for("tickets.task_detail", task_id=task.id))


@tickets_bp.route("/tasks/<int:task_id>/work_state", methods=["POST"])
@login_required
def toggle_task_work_state(task_id):
    task = TicketTask.query.get_or_404(task_id)
    closed_redirect = _ensure_task_not_closed(task)
    if closed_redirect:
        return closed_redirect
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if task.assigned_engineer_id != current_user.id and current_user.role != UserRole.ADMIN:
        abort(403)

    action = (request.form.get("action") or "").strip()
    user_name = current_user.full_name or current_user.username
    completion_notifications = []
    if action == "start":
        was_paused = _task_work_session_state(task) == "paused"
        if not task.target_date:
            target_date_raw = (request.form.get("target_date") or "").strip()
            if not target_date_raw:
                flash("Target schedule is required before starting work.", "warning")
                return redirect(url_for("tickets.task_detail", task_id=task.id))
            try:
                task.target_date = datetime.strptime(target_date_raw, "%Y-%m-%d").date()
            except ValueError:
                flash("Invalid target schedule format.", "danger")
                return redirect(url_for("tickets.task_detail", task_id=task.id))
            _record_task_change(task, f"Target schedule changed from Not set to {task.target_date.strftime('%Y-%m-%d')} by {user_name}.")
        if not _active_task_work_session(task):
            db.session.add(
                TicketTaskWorkSession(
                    ticket_task_id=task.id,
                    developer_id=current_user.id,
                    started_at=datetime.now(APP_TIMEZONE).replace(tzinfo=None),
                )
            )
        task.is_working = True
        if not task.started_date:
            task.started_date = date.today()
        if task.status in (TicketStatus.OPEN, TicketStatus.REOPENED, TicketStatus.ON_HOLD):
            task.status = TicketStatus.IN_PROGRESS
        parent_ticket = task.parent_ticket
        if parent_ticket and parent_ticket.status in (TicketStatus.OPEN, TicketStatus.REOPENED, TicketStatus.ON_HOLD):
            old_parent_status = parent_ticket.status
            parent_ticket.status = TicketStatus.IN_PROGRESS
            parent_ticket.kanban_bucket = None
            _record_ticket_change(
                parent_ticket,
                f"Status changed from {_enum_label(old_parent_status)} to In Progress by {user_name}.",
                is_internal=True,
            )
        _record_task_change(task, f"Work status changed to {'Resumed' if was_paused else 'Working'} by {user_name}.")
    elif action == "pause":
        active_session = _active_task_work_session(task)
        if not active_session and task.is_working:
            now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
            active_session = TicketTaskWorkSession(
                ticket_task_id=task.id,
                developer_id=current_user.id,
                started_at=now,
            )
            db.session.add(active_session)
            db.session.flush()
        if not active_session:
            flash("No active work session to pause.", "warning")
            return redirect(url_for("tickets.task_detail", task_id=task.id))
        active_session.paused_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        active_session.pause_type = "manual"
        active_session.pause_reason = (request.form.get("pause_reason") or "Manual pause").strip()
        task.is_working = False
        _record_task_change(task, f"Work status changed to Paused by {user_name}.")
    elif action == "stop":
        active_session = _active_task_work_session(task)
        if not active_session and task.is_working:
            now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
            active_session = TicketTaskWorkSession(
                ticket_task_id=task.id,
                developer_id=current_user.id,
                started_at=now,
            )
            db.session.add(active_session)
            db.session.flush()
        if active_session:
            active_session.ended_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        task.is_working = False
        _record_task_change(task, f"Work status changed to Done Work by {user_name}.")
        if task.status != TicketStatus.RESOLVED:
            old_status = task.status
            task.status = TicketStatus.RESOLVED
            task.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
            task.kanban_bucket = None
            _record_task_change(
                task,
                f"Status changed from {_enum_label(old_status)} to Fix/Completed by {user_name}.",
            )
            completion_notifications = queue_task_completed_notifications(task, actor=current_user)
    else:
        flash("Invalid work action.", "danger")
        return redirect(url_for("tickets.task_detail", task_id=task.id))

    db.session.commit()
    for recipient, notification in completion_notifications:
        emit_header_notification_added(recipient, notification)
    _emit_ticket_changed(task, "updated")
    if action == "start" and task.parent_ticket:
        _emit_ticket_changed(task.parent_ticket, "updated")
    flash("Work status updated.", "success")
    return redirect(url_for("tickets.task_detail", task_id=task.id))


@tickets_bp.route("/tasks/<int:task_id>/resolution", methods=["POST"])
@login_required
def resolve_task_decision(task_id):
    task = TicketTask.query.get_or_404(task_id)
    closed_redirect = _ensure_task_not_closed(task)
    if closed_redirect:
        return closed_redirect
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if current_user.role != UserRole.ADMIN and not _is_support_user(current_user):
        abort(403)

    action = (request.form.get("resolution_action") or "").strip().lower()
    if task.status != TicketStatus.RESOLVED:
        flash("Task resolution actions are only available when the task is Fix/Completed.", "warning")
        return redirect(url_for("tickets.task_detail", task_id=task.id))

    user_name = current_user.full_name or current_user.username
    active_session = _active_task_work_session(task)
    if active_session:
        active_session.ended_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    task.is_working = False

    if action == "accept":
        old_status = task.status
        task.status = TicketStatus.CLOSED
        task.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        task.kanban_bucket = None
        _record_task_change(
            task,
            f"Status changed from {_enum_label(old_status)} to Closed by {user_name}.",
        )
        flash("Task accepted and closed.", "success")
    elif action == "deny":
        old_status = task.status
        task.status = TicketStatus.REOPENED
        task.closed_at = None
        task.kanban_bucket = None
        _record_task_change(
            task,
            f"Status changed from {_enum_label(old_status)} to Re-Open by {user_name}.",
        )
        flash("Task denied and reopened.", "success")
    else:
        flash("Invalid task resolution action.", "danger")
        return redirect(url_for("tickets.task_detail", task_id=task.id))

    db.session.commit()
    _emit_ticket_changed(task, "updated")
    return redirect(url_for("tickets.task_detail", task_id=task.id))


@tickets_bp.route("/tasks/workday_prompt_state")
@login_required
def task_workday_prompt_state():
    if current_user.role in READ_ONLY_ROLES:
        return jsonify({"count": 0, "tasks": [], "prompt_date": None, "is_overdue": False})

    now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    prompt_cutoff = _current_workday_prompt_cutoff(now)
    tasks = [
        task
        for task in _current_user_working_tasks_query().all()
        if _task_is_eligible_for_workday_prompt(task, prompt_cutoff)
    ][:5]
    return jsonify(
        {
            "count": len(tasks),
            "prompt_date": prompt_cutoff.date().strftime("%Y-%m-%d"),
            "is_overdue": now < datetime.combine(now.date(), time(17, 30)),
            "tasks": [
                {
                    "id": task.id,
                    "task_no": task.task_no or generate_task_ticket_no(task.id),
                    "subject": task.subject or "Untitled task",
                    "url": url_for("tickets.task_detail", task_id=task.id),
                }
                for task in tasks
            ],
        }
    )


@tickets_bp.route("/tasks/workday_prompt_pause", methods=["POST"])
@login_required
def task_workday_prompt_pause():
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    tasks = _current_user_working_tasks_query().all()
    paused_count = 0
    user_name = current_user.full_name or current_user.username
    now = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    prompt_date_raw = (request.form.get("prompt_date") or "").strip()
    pause_cutoff = now
    if prompt_date_raw:
        try:
            prompt_date = datetime.strptime(prompt_date_raw, "%Y-%m-%d").date()
            pause_cutoff = datetime.combine(prompt_date, time(17, 30))
        except ValueError:
            pause_cutoff = now
    for task in tasks:
        active_session = _active_task_work_session(task)
        if not active_session:
            active_session = TicketTaskWorkSession(
                ticket_task_id=task.id,
                developer_id=current_user.id,
                started_at=now,
            )
            db.session.add(active_session)
            db.session.flush()
        effective_pause_at = pause_cutoff
        if effective_pause_at > now:
            effective_pause_at = now
        if active_session.started_at and effective_pause_at < active_session.started_at:
            effective_pause_at = active_session.started_at
        active_session.paused_at = effective_pause_at
        active_session.pause_type = "schedule"
        active_session.pause_reason = "Paused by 5:30 PM workday prompt"
        task.is_working = False
        _record_task_change(task, f"Work status changed to Paused by {user_name}.")
        paused_count += 1

    if paused_count:
        db.session.commit()
        for task in tasks:
            _emit_ticket_changed(task, "updated")
    else:
        db.session.rollback()

    return jsonify({"paused_count": paused_count})


@tickets_bp.route("/<int:ticket_id>/update-info", methods=["POST"])
@login_required
def update_ticket_info(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect

    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    changed = False
    user_name = current_user.full_name or current_user.username
    closed_child_tasks = []
    support_can_edit_core_fields = _can_edit_core_ticket_fields(current_user)
    # =========================
    # TARGET DATE
    # =========================
    new_target_date_raw = (request.form.get("target_date") or "").strip()
    old_target_date = ticket.target_date
    new_target_date = None

    if new_target_date_raw:
        try:
            new_target_date = datetime.strptime(new_target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid target date format.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    if old_target_date != new_target_date:
        ticket.target_date = new_target_date
        _record_ticket_change(
            ticket,
            f"Target schedule changed from {old_target_date.strftime('%Y-%m-%d') if old_target_date else 'Not set'} to {new_target_date.strftime('%Y-%m-%d') if new_target_date else 'Not set'} by {user_name}."
        )
        changed = True
        
    # =========================
    # DATE NEEDED
    # =========================
    if "date_needed" in request.form:
        new_date_needed_raw = (request.form.get("date_needed") or "").strip()
        old_date_needed = ticket.date_needed
        new_date_needed = None
        old_date_needed_date = old_date_needed.date() if old_date_needed else None

        if new_date_needed_raw:
            try:
                parsed_date_needed_date = datetime.strptime(new_date_needed_raw, "%Y-%m-%d").date()
                preserved_time = old_date_needed.time() if old_date_needed else time.min
                new_date_needed = datetime.combine(parsed_date_needed_date, preserved_time)
            except ValueError:
                flash("Invalid Date Needed format.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        new_date_needed_date = new_date_needed.date() if new_date_needed else None
        if old_date_needed_date != new_date_needed_date:
            ticket.date_needed = new_date_needed
            _record_ticket_change(
                ticket,
                f"Date needed changed from {old_date_needed.strftime('%Y-%m-%d') if old_date_needed else 'Not set'} to {new_date_needed.strftime('%Y-%m-%d') if new_date_needed else 'Not set'} by {user_name}."
            )
            changed = True

    # =========================
    # PRIORITY
    # =========================
    new_priority_raw = (request.form.get("priority") or "").strip()
    valid_priorities = {
        TicketPriority.NOT_SET.value: TicketPriority.NOT_SET,
        TicketPriority.CRITICAL.value: TicketPriority.CRITICAL,
        TicketPriority.HIGH.value: TicketPriority.HIGH,
        TicketPriority.MEDIUM.value: TicketPriority.MEDIUM,
        TicketPriority.LOW.value: TicketPriority.LOW,
    }

    new_priority = valid_priorities.get(new_priority_raw)
    if new_priority and ticket.priority != new_priority:
        old_priority = ticket.priority
        ticket.priority = new_priority
        _record_ticket_change(
            ticket,
            f"Priority changed from {_enum_label(old_priority)} to {_enum_label(new_priority)} by {user_name}."
        )
        changed = True

    # =========================
    # STATUS
    # =========================
    new_status_raw = (request.form.get("status") or "").strip()
    valid_statuses = {
        TicketStatus.OPEN.value: TicketStatus.OPEN,
        TicketStatus.IN_PROGRESS.value: TicketStatus.IN_PROGRESS,
        TicketStatus.RESOLVED.value: TicketStatus.RESOLVED,
        TicketStatus.REOPENED.value: TicketStatus.REOPENED,
        TicketStatus.CLOSED.value: TicketStatus.CLOSED,
        TicketStatus.CANCELLED.value: TicketStatus.CANCELLED,
    }

    new_status = valid_statuses.get(new_status_raw)
    if not new_status:
        flash("Invalid status selection.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    if ticket.status != new_status:
        old_status = ticket.status
        if (
            new_status == TicketStatus.CLOSED
            and ticket.reported_by
            and ticket.reported_by.role in CLIENT_SCOPED_ROLES
            and not ticket.signature_attachment
        ):
            signature_data = request.form.get("signature_data") or ""
            if not signature_data:
                flash("Client signature is required before closing tickets reported by a client.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            try:
                header, encoded = signature_data.split(",", 1) if "," in signature_data else ("", signature_data)
                sig_bytes = base64.b64decode(encoded)
            except Exception:
                flash("Invalid signature data. Please recapture the signature.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
            if not upload_folder:
                flash("Signature upload folder is not configured.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            os.makedirs(upload_folder, exist_ok=True)
            filename = f"{ticket.id}_signature_{int(datetime.utcnow().timestamp())}.png"
            filepath = os.path.join(upload_folder, filename)

            with open(filepath, "wb") as f:
                f.write(sig_bytes)

            attachment = TicketAttachment(
                ticket_id=ticket.id,
                user_id=current_user.id,
                stored_filename=filename,
                original_filename="signature.png",
                content_type="image/png",
                file_size=len(sig_bytes),
            )
            db.session.add(attachment)

        ticket.status = new_status

        if new_status in (TicketStatus.RESOLVED, TicketStatus.CLOSED):
            ticket.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        elif new_status == TicketStatus.REOPENED:
            ticket.closed_at = None
        _record_ticket_change(
            ticket,
            f"Status changed from {_enum_label(old_status)} to {_enum_label(new_status)} by {user_name}."
        )
        if new_status == TicketStatus.CLOSED:
            closed_child_tasks = _close_child_tasks(ticket, user_name)
        changed = True

    # =========================
    # ASSIGNEE
    # =========================
    if "engineer_id" in request.form:
        engineer_id_raw = (request.form.get("engineer_id") or "").strip()
        old_engineer = ticket.assigned_engineer

        if engineer_id_raw == "":
            new_engineer = None
        else:
            new_engineer = User.query.filter(
                User.id == engineer_id_raw,
                User.role == UserRole.ENGINEER
            ).first()

            if not new_engineer:
                flash("Invalid engineer selection.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        old_engineer_id = old_engineer.id if old_engineer else None
        new_engineer_id = new_engineer.id if new_engineer else None

        if old_engineer_id != new_engineer_id:
            ticket.assigned_engineer_id = new_engineer_id
            ticket.assigned_by_id = current_user.id

            # if ticket.status == TicketStatus.OPEN and new_engineer_id:
            #     ticket.status = TicketStatus.IN_PROGRESS
            if new_engineer:
                _record_ticket_change(
                    ticket,
                    f"Engineer/IT assigned to {new_engineer.full_name or new_engineer.username} (previously {old_engineer.full_name or old_engineer.username if old_engineer else 'None'}) by {user_name}."
                )
            else:
                _record_ticket_change(
                    ticket,
                    f"Engineer/IT assignment cleared (previously {old_engineer.full_name or old_engineer.username if old_engineer else 'None'}) by {user_name}."
                )
            changed = True

    if any(key in request.form for key in ("subject", "description", "client_id", "app_id")):
        if not support_can_edit_core_fields:
            abort(403)

        subject = (request.form.get("subject") or "").strip()
        description = (request.form.get("description") or "").strip()
        client_id_raw = (request.form.get("client_id") or "").strip()
        app_id_raw = (request.form.get("app_id") or "").strip()

        if not subject:
            flash("Title is required.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))
        if len(subject) > 100:
            flash("Title must be 100 characters or fewer.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))
        if not client_id_raw:
            flash("Client is required.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))
        try:
            client_id = int(client_id_raw)
        except ValueError:
            flash("Invalid client selection.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))
        client = Client.query.get(client_id)
        if not client:
            flash("Invalid client selection.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        normalized_description = description or None
        if ticket.subject != subject or (ticket.description or None) != normalized_description:
            ticket.subject = subject
            ticket.description = normalized_description
            _record_ticket_change(ticket, f"Title/description updated by {user_name}.")
            changed = True

        if ticket.client_id != client.id:
            previous_client = ticket.client.name if ticket.client else "None"
            ticket.client_id = client.id
            if ticket.instrument_id and ticket.instrument and ticket.instrument.client_id != client.id:
                ticket.instrument_id = None
            if ticket.app_id and ticket.app and client not in ticket.app.clients:
                ticket.app_id = None
            _record_ticket_change(ticket, f"Client changed from {previous_client} to {client.name} by {user_name}.")
            changed = True

        if app_id_raw:
            try:
                app_id = int(app_id_raw)
            except ValueError:
                flash("Invalid application selection.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))
            app_obj = App.query.get(app_id)
            if not app_obj:
                flash("Invalid application selection.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))
            if client not in app_obj.clients:
                flash("Selected application does not belong to the selected client.", "danger")
                return redirect(url_for("tickets.detail", ticket_id=ticket.id))

            if ticket.app_id != app_obj.id or ticket.ticket_for != "app":
                previous_app = ticket.app.name if ticket.app else "None"
                ticket.app_id = app_obj.id
                ticket.ticket_for = "app"
                ticket.instrument_id = None
                _record_ticket_change(ticket, f"Application changed from {previous_app} to {app_obj.name} by {user_name}.")
                changed = True

    if changed:
        db.session.commit()
        _emit_ticket_changed(ticket, "updated")
        for child_task in closed_child_tasks:
            _emit_ticket_changed(child_task, "updated")
        flash("Ticket info updated successfully.", "success")
    else:
        flash("No changes detected.", "warning")

    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/update-client-fields", methods=["POST"])
@login_required
def update_client_fields(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)
    _ensure_client_ticket_access(ticket)
    closed_redirect = _ensure_ticket_not_closed(ticket)
    if closed_redirect:
        return closed_redirect

    if current_user.role not in CLIENT_SCOPED_ROLES:
        abort(403)

    subject = (request.form.get("subject") or "").strip()
    description = (request.form.get("description") or "").strip()

    if not subject:
        flash("Complaint is required.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    if len(subject) > 100:
        flash("Complaint must be 100 characters or fewer.", "danger")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    changed = False
    user_name = current_user.full_name or current_user.username

    if ticket.subject != subject:
        ticket.subject = subject
        changed = True

    normalized_description = description or None
    if (ticket.description or None) != normalized_description:
        ticket.description = normalized_description
        changed = True

    if changed:
        _record_ticket_change(ticket, f"Complaint/details updated by {user_name}.")
        db.session.commit()
        _emit_ticket_changed(ticket, "updated")
        flash("Ticket updated successfully.", "success")
    else:
        flash("No changes detected.", "warning")

    return redirect(url_for("tickets.detail", ticket_id=ticket.id))


@tickets_bp.route("/<int:ticket_id>/comment-templates/create", methods=["POST"])
@login_required
def create_comment_template(ticket_id):
    ticket = Ticket.query.get_or_404(ticket_id)

    if not _can_manage_comment_templates(current_user):
        abort(403)

    template = (request.form.get("template") or "").strip()
    is_exclusive = bool(request.form.get("is_exclusive"))

    if not template:
        flash("Template is required.", "warning")
        return redirect(url_for("tickets.detail", ticket_id=ticket.id))

    comment_template = CommentTemplate(
        template=template,
        is_exclusive=is_exclusive,
        is_active=True,
        created_at=datetime.now(APP_TIMEZONE).replace(tzinfo=None),
        created_by=current_user.id,
    )

    db.session.add(comment_template)
    db.session.commit()

    flash("Comment template saved.", "success")
    return redirect(f"{url_for('tickets.detail', ticket_id=ticket.id)}#ticket-comments-card")