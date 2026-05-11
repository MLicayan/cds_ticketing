from werkzeug.utils import secure_filename
import os
import shutil
import csv
import io
import calendar
import base64
import json
from collections import defaultdict
from datetime import datetime, date, timedelta, time

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, current_app, abort, jsonify
from flask_login import login_required, current_user

from . import APP_TIMEZONE, db, socketio, to_localtime
from .models import (
    Ticket,
    Client,
    Instrument,
    TicketStatus,
    TicketPriority,
    TicketComment,
    TicketAttachment,
    TicketTask,
    TicketTaskAttachment,
    TicketTaskComment,
    UserRole,
    User,
    App,
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
        "tickets.task_detail",
        "tickets.update_task_info",
        "tickets.toggle_task_work_state",
        "tickets.react_task_comment",
    }
    if request.endpoint in task_endpoints:
        if not (current_user.has_nav_access("developer_tasks") or current_user.has_nav_access("my_tickets")):
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


def _ensure_ticket_not_closed(ticket: Ticket):
    if ticket.status == TicketStatus.CLOSED:
        flash("Closed tickets are read-only.", "warning")
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
    return {
        "ticket_id": comment.ticket_id,
        "comment_id": comment.id,
        "user_id": comment.user_id,
        "user": comment.user.full_name or comment.user.username if comment.user else "",
        "comment_text": comment.comment_text,
        "is_internal": bool(comment.is_internal),
        "user_is_client": bool(comment.user and comment.user.role in CLIENT_SCOPED_ROLES),
        "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
        "reactions": comment.reaction_summary(),
        "attachments": [_ticket_attachment_payload(att) for att in (attachments or [])],
    }


def _task_comment_payload(comment: TicketTaskComment, attachments=None) -> dict:
    return {
        "ticket_id": comment.ticket_task_id,
        "comment_id": comment.id,
        "user_id": comment.user_id,
        "user": comment.user.full_name or comment.user.username if comment.user else "",
        "comment_text": comment.comment_text,
        "is_internal": bool(comment.is_internal),
        "user_is_client": bool(comment.user and comment.user.role in CLIENT_SCOPED_ROLES),
        "created_at": to_localtime(comment.created_at).strftime("%Y-%m-%d %H:%M") if comment.created_at else "",
        "reactions": comment.reaction_summary(),
        "attachments": [_ticket_attachment_payload(att) for att in (attachments or [])],
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
    my_state = reaction_state_map.get(str(current_user.id), {})
    return {
        "ticket_id": comment.ticket_task_id,
        "comment_id": comment.id,
        "reactions": comment.reaction_summary(),
        "my_reaction": my_state.get("reaction", ""),
        "my_acknowledged": bool(my_state.get("acknowledge")),
    }


def _is_support_user(user: User) -> bool:
    return bool(
        user
        and user.role == UserRole.ENGINEER
        and (user.user_type or "").strip().lower() == "support"
    )


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
    if not comment or not user or comment.is_internal:
        return False
    if comment.user_id == user.id:
        return False
    if _is_change_comment(comment.comment_text):
        return False

    commenter_is_client = bool(comment.user and comment.user.role in CLIENT_SCOPED_ROLES)
    viewer_is_client = user.role in CLIENT_SCOPED_ROLES
    if _is_support_user(user):
        return True
    if commenter_is_client == viewer_is_client:
        return False

    ticket = comment.ticket
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
    if not comment.user:
        return []

    user_ids = set()
    support_users = User.query.filter(
        User.role == UserRole.ENGINEER,
        db.func.lower(User.user_type) == "support",
    ).all()
    user_ids.update(user.id for user in support_users)

    user_ids.discard(comment.user_id)
    if not user_ids:
        return []
    recipients = User.query.filter(User.id.in_(user_ids)).all()
    return [
        recipient
        for recipient in recipients
        if _is_ticket_comment_notification_for_user(comment, recipient)
    ]


def _ticket_comment_notification_count_for_user(user: User) -> int:
    if not user:
        return 0

    query = (
        TicketComment.query
        .join(Ticket, TicketComment.ticket_id == Ticket.id)
        .join(User, TicketComment.user_id == User.id)
        .filter(TicketComment.is_internal.is_(False), TicketComment.user_id != user.id)
    )

    if _is_support_user(user):
        pass
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

    total = 0
    for comment in query.order_by(TicketComment.created_at.desc()).limit(100).all():
        if not _is_ticket_comment_notification_for_user(comment, user):
            continue
        if comment.reaction_state_map().get(str(user.id), {}).get("acknowledge"):
            continue
        total += 1
    return total


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
    for recipient in _notification_recipients_for_comment(ticket, comment):
        socketio.emit(
            "ticket_comment_notification_added",
            _header_notification_payload(comment, recipient),
            room=f"user_notifications:{recipient.id}",
        )


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
    if _is_support_user(current_user):
        return query
    if current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
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
    using_default_open_not_set_scope = (
        current_user.role not in CLIENT_SCOPED_ROLES
        and not my_tickets_only
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
        query = query.filter(list_model.ticket_no.ilike(f"%{ticket_no_raw}%"))

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

    valid_status_values = []
    for status_raw in effective_status_values:
        try:
            valid_status_values.append(TicketStatus(status_raw))
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

        if valid_status_values:
            query = query.filter(list_model.status.in_(valid_status_values))
        else:
            query = query.filter(list_model.status != TicketStatus.CLOSED)

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

    tickets = query.order_by(list_model.created_at.desc()).all()
    now = datetime.now(APP_TIMEZONE)
    current_date = now.strftime("%Y-%m-%d")

    return render_template(
        "tickets/index.html",
        tickets=tickets,
        now=now,
        current_date=current_date,
        status_labels=status_labels,
        clients=clients,
        instruments=instruments,
        apps=apps,
        assignees=assignees,
        reporters=reporters,
        ticket_list_title="My Task" if my_tickets_only else "All Tickets",
        ticket_list_endpoint="tickets.my_tickets" if my_tickets_only else "tickets.index",
        is_task_list=is_task_list,
        selected_filters={
            "client_ids": client_ids,
            "instrument_ids": instrument_ids,
            "app_ids": app_ids,
            "ticket_no": ticket_no_raw,
            "assignee_id": assignee_id,
            "reported_by_ids": reported_by_ids,
            "priority": [] if using_default_open_not_set_scope else priority_values,
            "status": [] if using_default_open_not_set_scope else [status.value for status in valid_status_values],
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

    filter_by = request.args.get("filter_by", "").strip()
    filter_value = request.args.get("filter_value", "").strip()
    filter_user_type = request.args.get("filter_user_type", "").strip().upper()

    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    clients = Client.query.order_by(Client.name.asc()).all()

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

    if filter_by == "my":
        base_query = base_query.filter(Ticket.assigned_engineer_id == current_user.id)

    elif filter_by == "assigned":
        if filter_value == "unassigned":
            base_query = base_query.filter(Ticket.assigned_engineer_id.is_(None))
        elif filter_value:
            try:
                base_query = base_query.filter(Ticket.assigned_engineer_id == int(filter_value))
            except ValueError:
                pass

        if filter_user_type:
            base_query = base_query.join(Ticket.assigned_engineer).filter(
                db.func.upper(User.user_type) == filter_user_type
            )

    elif filter_by == "client":
        if filter_value:
            try:
                base_query = base_query.filter(Ticket.client_id == int(filter_value))
            except ValueError:
                pass

    tickets = base_query.order_by(Ticket.created_at.desc()).all()

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
        selected_filter={
            "filter_by": filter_by,
            "filter_value": filter_value,
            "filter_user_type": filter_user_type,
        },
    )


@tickets_bp.route("/gantt")
@login_required
def gantt_view():
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

    query = Ticket.query
    if current_user.role in READ_ONLY_ROLES:
        query = _exclude_task_tickets(query)
    if current_user.role in CLIENT_SCOPED_ROLES:
        query = _apply_client_ticket_scope(query)
    elif current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
        query = query.filter(Ticket.assigned_engineer_id == current_user.id)

    tickets = query.order_by(Ticket.created_at.asc()).all()

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
    )


@tickets_bp.route("/calendar")
@login_required
def calendar_view():
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

    if filter_by == "my":
        base_query = base_query.filter(Ticket.assigned_engineer_id == current_user.id)
    elif filter_by == "engineer" and filter_value:
        if filter_value == "unassigned":
            base_query = base_query.filter(Ticket.assigned_engineer_id.is_(None))
        else:
            try:
                base_query = base_query.filter(Ticket.assigned_engineer_id == int(filter_value))
            except ValueError:
                pass
    elif filter_by == "client" and filter_value:
        try:
            base_query = base_query.filter(Ticket.client_id == int(filter_value))
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
        base_query.filter(Ticket.target_date.isnot(None))
        .filter(Ticket.target_date >= month_start)
        .filter(Ticket.target_date < next_month)
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

        db.session.commit()
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
    assignees = User.query.filter(
        User.role == UserRole.ENGINEER,
        User.user_type == "IT",
    ).order_by(User.full_name.asc(), User.username.asc()).all()

    query = TicketTask.query
    if current_user.role == UserRole.ENGINEER and not _is_support_user(current_user):
        query = query.filter(TicketTask.assigned_engineer_id == current_user.id)

    client_ids = [cid.strip() for cid in request.args.getlist("client_id") if cid.strip()]
    assignee_ids = [aid.strip() for aid in request.args.getlist("assignee_id") if aid.strip()]
    priority_values = [priority.strip() for priority in request.args.getlist("priority") if priority.strip()]
    status_values = [status.strip() for status in request.args.getlist("status") if status.strip()]
    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""

    if client_ids:
        try:
            query = query.filter(TicketTask.client_id.in_([int(cid) for cid in client_ids]))
        except ValueError:
            pass

    if assignee_ids and (current_user.role != UserRole.ENGINEER or _is_support_user(current_user)):
        try:
            query = query.filter(TicketTask.assigned_engineer_id.in_([int(aid) for aid in assignee_ids]))
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

    valid_status_values = []
    for status_raw in status_values:
        try:
            valid_status_values.append(TicketStatus(status_raw))
        except ValueError:
            continue
    if valid_status_values:
        query = query.filter(TicketTask.status.in_(valid_status_values))
    else:
        query = query.filter(TicketTask.status != TicketStatus.CLOSED)

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

    parents = {}
    if parent_ids:
        parents = {ticket.id: ticket for ticket in Ticket.query.filter(Ticket.id.in_(parent_ids)).all()}

    return render_template(
        "tickets/tasks.html",
        tasks=tasks,
        parents=parents,
        status_labels=status_labels,
        clients=clients,
        assignees=assignees,
        current_date=datetime.now(APP_TIMEZONE).strftime("%Y-%m-%d"),
        selected_filters={
            "client_ids": client_ids,
            "assignee_ids": assignee_ids,
            "priority": priority_values,
            "status": [status.value for status in valid_status_values],
            "date_from": date_from_raw,
            "date_to": date_to_raw,
        },
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

    is_task_ticket = _is_task_ticket(ticket)
    parent_ticket = None
    if is_task_ticket:
        try:
            parent_ticket_id = int((ticket.category or "").replace(TASK_CATEGORY_PREFIX, "", 1))
            parent_ticket = Ticket.query.get(parent_ticket_id)
        except ValueError:
            parent_ticket = None

    today_str = datetime.utcnow().strftime("%Y-%m-%d")
    comments_raw = ticket.comments
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
        task_engineers = User.query.filter(
            User.role == UserRole.ENGINEER,
            User.user_type == "IT"
        ).order_by(User.full_name.asc(), User.username.asc()).all()

    ticket_tasks = TicketTask.query.filter(TicketTask.ticket_id == ticket.id).order_by(
        TicketTask.created_at.desc(),
        TicketTask.id.desc()
    ).all()

    timeline_events = []

    def add_event(timestamp, title, description, kind="update"):
        if not timestamp:
            return
        timeline_events.append(
            {
                "timestamp": timestamp,
                "title": title,
                "description": description,
                "type": kind,
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
        is_internal = False if current_user.role in READ_ONLY_ROLES else bool(request.form.get("is_internal"))
        files = request.files.getlist("attachment")
        has_uploaded_files = any(file and file.filename for file in files)
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        did_something = False
        new_comment = None
        added_attachments = []
        propagated_child_tasks = []

        if comment_text or has_uploaded_files:
            comment = TicketComment(
                ticket_id=ticket.id,
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
    add_event(ticket.created_at, "Ticket created", f"Reported by {reported_by_name}.", "created")

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
        )

    for att in ticket.attachments:
        if _is_initial_attachment(att):
            continue
        sender = att.user.full_name or att.user.username if att.user else "Unknown"
        add_event(
            att.uploaded_at,
            f"Attachment added by {sender}",
            att.original_filename,
            "attachment",
        )

    for log in ticket.service_logs:
        visit_dt = None
        if log.visit_date:
            visit_dt = datetime.combine(log.visit_date, log.start_time or time(12, 0))
        else:
            visit_dt = log.created_at

        engineer_name = log.engineer.full_name or log.engineer.username
        log_type = (log.service_type or "Service").title()
        description = f"{engineer_name} recorded a {log_type.lower()} visit."
        add_event(visit_dt, f"Service log ({log_type})", description, "log")

    if ticket.status == TicketStatus.CLOSED:
        add_event(ticket.closed_at, "Ticket closed", "Marked as closed.", "closed")
    elif ticket.status == TicketStatus.RESOLVED:
        add_event(ticket.updated_at, "Ticket resolved", "Awaiting confirmation.", "resolved")

    timeline_events.sort(key=lambda e: e["timestamp"] or datetime.min, reverse=True)

    comment_attachments = {}
    assigned_attachment_ids = set()
    non_initial_attachments = [att for att in ticket.attachments if not _is_initial_attachment(att)]
    for comment in comments:
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
    ]
    status_labels = {k: v for k, v in status_options}
    priority_options = [
        (TicketPriority.NOT_SET, "Not Set"),
        (TicketPriority.CRITICAL, "Critical"),
        (TicketPriority.HIGH, "High"),
        (TicketPriority.MEDIUM, "Medium"),
        (TicketPriority.LOW, "Low"),
    ]

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
        TicketStatus=TicketStatus,
        default_target_date=today_str,
        timeline_events=timeline_events,
        comment_attachments=comment_attachments,
        remaining_attachments=remaining_attachments,
    )


@tickets_bp.route("/tasks/<int:task_id>", methods=["GET", "POST"])
@login_required
def task_detail(task_id):
    task = TicketTask.query.get_or_404(task_id)
    parent_ticket = task.parent_ticket
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if (
        current_user.role == UserRole.ENGINEER
        and not _is_support_user(current_user)
        and task.assigned_engineer_id != current_user.id
    ):
        abort(403)

    if request.method == "POST":
        closed_redirect = _ensure_task_not_closed(task)
        if closed_redirect:
            return closed_redirect

        comment_text = (request.form.get("comment_text") or "").strip()
        is_internal = bool(request.form.get("is_internal"))
        files = request.files.getlist("attachment")
        has_uploaded_files = any(file and file.filename for file in files)
        wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
        did_something = False
        new_comment = None
        added_attachments = []

        if comment_text or has_uploaded_files:
            new_comment = TicketTaskComment(
                ticket_task_id=task.id,
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
            db.session.commit()
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
    comments_raw = task.comments
    comments = [c for c in comments_raw if not _is_change_comment(c.comment_text)]

    task_engineers = User.query.filter(
        User.role == UserRole.ENGINEER,
        User.user_type == "IT"
    ).order_by(User.full_name.asc(), User.username.asc()).all()
    engineers = task_engineers

    timeline_events = []

    def add_event(timestamp, title, description, kind="update"):
        if not timestamp:
            return
        timeline_events.append({"timestamp": timestamp, "title": title, "description": description, "type": kind})

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
    add_event(task.created_at, "Task created", f"Created by {reported_by_name}.", "created")

    for c in comments_raw:
        if not _is_change_comment(c.comment_text):
            continue
        commenter = c.user.full_name or c.user.username
        add_event(c.created_at, f"Updated by {commenter}", c.comment_text, "update")

    for att in task.attachments:
        if _is_initial_attachment(att):
            continue
        sender = att.user.full_name or att.user.username if att.user else "Unknown"
        add_event(att.uploaded_at, f"Attachment added by {sender}", att.original_filename, "attachment")

    if task.status == TicketStatus.CLOSED:
        add_event(task.closed_at, "Task closed", "Marked as closed.", "closed")
    elif task.status == TicketStatus.RESOLVED:
        add_event(task.updated_at, "Task resolved", "Awaiting confirmation.", "resolved")

    timeline_events.sort(key=lambda e: e["timestamp"] or datetime.min, reverse=True)

    comment_attachments = {}
    assigned_attachment_ids = set()
    non_initial_attachments = [att for att in task.attachments if not _is_initial_attachment(att)]
    for comment in comments:
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
    ]
    priority_options = [
        (TicketPriority.NOT_SET, "Not Set"),
        (TicketPriority.CRITICAL, "Critical"),
        (TicketPriority.HIGH, "High"),
        (TicketPriority.MEDIUM, "Medium"),
        (TicketPriority.LOW, "Low"),
    ]

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
        TicketStatus=TicketStatus,
        default_target_date=today_str,
        timeline_events=timeline_events,
        comment_attachments=comment_attachments,
        remaining_attachments=remaining_attachments,
    )


@tickets_bp.route("/comments/<int:comment_id>/react", methods=["POST"])
@login_required
def react_comment(comment_id):
    comment = TicketComment.query.get_or_404(comment_id)
    ticket = comment.ticket

    _ensure_client_ticket_access(ticket)
    if ticket.status == TicketStatus.CLOSED:
        return jsonify({"error": "Closed tickets are read-only."}), 403
    if current_user.role in READ_ONLY_ROLES and _is_task_ticket(ticket):
        abort(403)
    if comment.is_internal and current_user.role in READ_ONLY_ROLES:
        abort(403)

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
    can_create_tasks = (
        current_user.role == UserRole.ADMIN
        or (current_user.role == UserRole.ENGINEER and (current_user.user_type or "").strip().lower() == "support")
    )
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
            flash("Each task must be assigned to an IT user.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        try:
            engineer_id_int = int(engineer_id)
        except ValueError:
            flash("Invalid IT assignment selected.", "danger")
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        engineer = User.query.filter(
            User.id == engineer_id_int,
            User.role == UserRole.ENGINEER,
            User.user_type == "IT",
        ).first()
        if not engineer:
            flash("Invalid IT assignment selected.", "danger")
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
        )
        db.session.add(task_ticket)
        db.session.flush()
        task_ticket.task_no = generate_task_ticket_no(task_ticket.id)
        _clone_ticket_attachments(ticket, task_ticket, uploaded_at=task_ticket.created_at)
        created_task_tickets.append(task_ticket)

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
        return jsonify({"error": "Closed tickets are read-only."}), 403

    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    column_key = (request.form.get("column") or "").strip().lower()
    if not column_key:
        column_key = (request.form.get("status") or "").strip().lower()

    if column_key not in {"backlog", "open", "in_progress", "resolved", "currently_working"}:
        return jsonify({"error": "Invalid kanban status."}), 400

    actor_name = current_user.full_name or current_user.username
    changed = False

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
            t.reported_by.full_name or t.reported_by.username if t.reported_by else "",
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
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    changed = False
    user_name = current_user.full_name or current_user.username

    new_target_date_raw = (request.form.get("target_date") or "").strip()
    old_target_date = task.target_date
    new_target_date = None
    if new_target_date_raw:
        try:
            new_target_date = datetime.strptime(new_target_date_raw, "%Y-%m-%d").date()
        except ValueError:
            flash("Invalid target date format.", "danger")
            return redirect(url_for("tickets.task_detail", task_id=task.id))

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
    }
    new_status = valid_statuses.get((request.form.get("status") or "").strip())
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

    if "engineer_id" in request.form:
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

    if changed:
        db.session.commit()
        _emit_ticket_changed(task, "updated")
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
    if action == "start":
        task.is_working = True
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
        _record_task_change(task, f"Work status changed to Working by {user_name}.")
    elif action == "stop":
        task.is_working = False
        _record_task_change(task, f"Work status changed to Not Working by {user_name}.")
    else:
        flash("Invalid work action.", "danger")
        return redirect(url_for("tickets.task_detail", task_id=task.id))

    db.session.commit()
    _emit_ticket_changed(task, "updated")
    if action == "start" and task.parent_ticket:
        _emit_ticket_changed(task.parent_ticket, "updated")
    flash("Work status updated.", "success")
    return redirect(url_for("tickets.task_detail", task_id=task.id))


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
