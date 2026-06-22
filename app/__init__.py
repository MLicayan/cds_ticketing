from datetime import datetime, timedelta, timezone

from flask import Flask
from flask import url_for
from flask_login import current_user
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_socketio import SocketIO
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
socketio = SocketIO(
    cors_allowed_origins="*",
    async_mode="threading",
    logger=False,
    engineio_logger=False,
)
migrate = None
APP_TIMEZONE = timezone(timedelta(hours=8))


def format_reported_by_name(ticket_like, viewer=None):
    reporter = getattr(ticket_like, "reported_by", None)
    if not reporter:
        return ""

    reporter_name = reporter.full_name or reporter.username or ""
    viewer_role = getattr(viewer, "role", None)
    reporter_user_type = (getattr(reporter, "user_type", "") or "").strip().lower()

    viewer_role = getattr(viewer_role, "value", viewer_role)

    if viewer_role in ("client", "client_admin") and reporter_user_type == "support":
        return f"{reporter_name} - (CDS Support)"

    return reporter_name


def format_assigned_to_name(ticket_like):
    assigned_engineer = getattr(ticket_like, "assigned_engineer", None)
    tasks = getattr(ticket_like, "tasks", None)

    if tasks:
        seen = set()
        task_assignees = []
        for task in tasks:
            engineer = getattr(task, "assigned_engineer", None)
            if not engineer:
                continue
            engineer_name = engineer.full_name or engineer.username or ""
            if not engineer_name or engineer_name in seen:
                continue
            seen.add(engineer_name)
            task_assignees.append(engineer_name)
        if task_assignees:
            return ", ".join(task_assignees)

    if assigned_engineer:
        return assigned_engineer.full_name or assigned_engineer.username or "Unassigned"

    return "Unassigned"


def local_naive_to_localtime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=APP_TIMEZONE)
    return value.astimezone(APP_TIMEZONE)


def utc_naive_to_localtime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(APP_TIMEZONE)


def to_localtime(value):
    if value is None:
        return None
    if value.tzinfo is None:
        now_utc = datetime.utcnow()
        now_local = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
        if abs(now_local - value) <= abs(now_utc - value):
            value = value.replace(tzinfo=APP_TIMEZONE)
        else:
            value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(APP_TIMEZONE)


def _ticket_creation_notification_payload(notification):
    from .models import TicketStatus

    ticket = getattr(notification, "ticket", None)
    actor = getattr(notification, "actor", None)
    if not ticket:
        return None
    if getattr(ticket, "status", None) in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return None

    actor_name = ""
    if actor:
        actor_name = actor.full_name or actor.username or ""

    return {
        "id": notification.id,
        "type": notification.notification_type,
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "subject": ticket.subject,
        "reported_by": ticket.reported_by.full_name or ticket.reported_by.username if ticket.reported_by else "",
        "url": url_for("tickets.detail", ticket_id=ticket.id) + "#ticket-comments-card",
        "comment_id": None,
        "comment_text": notification.comment_preview or (f"New ticket created by {actor_name}." if actor_name else "New ticket created."),
        "user": actor_name or "System",
        "created_at": to_localtime(notification.created_at).strftime("%Y-%m-%d %H:%M") if notification.created_at else "",
        "count": 1,
    }


def _ticket_comment_notification_payload(notification):
    from .models import TicketStatus

    ticket = getattr(notification, "ticket", None)
    actor = getattr(notification, "actor", None)
    if not ticket:
        return None
    if getattr(ticket, "status", None) in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return None

    actor_name = ""
    if actor:
        actor_name = actor.full_name or actor.username or ""

    return {
        "id": notification.id,
        "type": notification.notification_type,
        "ticket_id": ticket.id,
        "ticket_no": ticket.ticket_no,
        "subject": ticket.subject,
        "reported_by": ticket.reported_by.full_name or ticket.reported_by.username if ticket.reported_by else "",
        "url": url_for("tickets.detail", ticket_id=ticket.id) + "#ticket-comments-card",
        "comment_id": None,
        "comment_text": notification.comment_preview or (f"New comment from {actor_name}." if actor_name else "New ticket comment."),
        "user": actor_name or "System",
        "created_at": to_localtime(notification.created_at).strftime("%Y-%m-%d %H:%M") if notification.created_at else "",
        "count": 1,
    }


def _task_notification_payload(notification):
    from .models import TicketStatus

    task = getattr(notification, "task", None)
    actor = getattr(notification, "actor", None)
    if not task:
        return None
    if getattr(task, "status", None) in (TicketStatus.CLOSED, TicketStatus.CANCELLED):
        return None

    actor_name = ""
    if actor:
        actor_name = actor.full_name or actor.username or ""

    notification_type = getattr(notification, "notification_type", "")
    default_comment_text = "New task notification."
    if notification_type == "task_comment":
        default_comment_text = f"New comment from {actor_name}." if actor_name else "New task comment."
    elif notification_type == "task_completed":
        default_comment_text = f"Task marked Fix/Completed by {actor_name}." if actor_name else "Task marked Fix/Completed."

    return {
        "id": notification.id,
        "type": notification_type,
        "ticket_id": task.id,
        "ticket_no": task.ticket_no,
        "subject": task.subject,
        "reported_by": task.reported_by.full_name or task.reported_by.username if task.reported_by else "",
        "url": url_for("tickets.task_detail", task_id=task.id) + "#ticket-comments-card",
        "comment_id": None,
        "comment_text": notification.comment_preview or default_comment_text,
        "user": actor_name or "System",
        "created_at": to_localtime(notification.created_at).strftime("%Y-%m-%d %H:%M") if notification.created_at else "",
        "count": 1,
    }


def header_notification_payload(notification):
    notification_type = getattr(notification, "notification_type", "")
    if notification_type == "ticket_comment":
        return _ticket_comment_notification_payload(notification)
    if notification_type in ("task_comment", "task_completed"):
        return _task_notification_payload(notification)
    return _ticket_creation_notification_payload(notification)


def build_header_notifications_for_user(user, limit: int = 8) -> dict:
    if not user:
        return {"count": 0, "notifications": []}

    from .models import TicketNotification

    notifications = []

    stored_notifications = (
        TicketNotification.query
        .filter(
            TicketNotification.recipient_id == user.id,
            TicketNotification.notification_type.in_(("ticket_created", "ticket_comment", "task_comment", "task_completed")),
            TicketNotification.read_at.is_(None),
        )
        .order_by(TicketNotification.created_at.desc(), TicketNotification.id.desc())
        .all()
    )
    for notification in stored_notifications:
        payload = header_notification_payload(notification)
        if payload:
            notifications.append(payload)

    notifications.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return {
        "count": len(notifications),
        "notifications": notifications[:limit],
    }


def queue_ticket_creation_notifications(ticket, actor=None):
    if not ticket:
        return []

    from .models import TicketNotification, User, UserRole

    actor_id = getattr(actor, "id", None)
    support_or_admin_users = (
        User.query.filter(
            User.is_active_user.is_(True),
            db.or_(
                User.role == UserRole.ADMIN,
                db.and_(
                    User.role == UserRole.ENGINEER,
                    db.func.lower(db.func.trim(User.user_type)) == "support",
                ),
            ),
        )
        .order_by(User.id.asc())
        .all()
    )

    recipients = []
    for recipient in support_or_admin_users:
        if actor_id and recipient.id == actor_id:
            continue
        notification = TicketNotification(
            ticket_id=ticket.id,
            task_id=None,
            recipient_id=recipient.id,
            actor_id=actor_id,
            notification_type="ticket_created",
            comment_preview=f"New ticket created by {actor.full_name or actor.username}." if actor else "New ticket created.",
            created_at=datetime.now(APP_TIMEZONE).replace(tzinfo=None),
        )
        db.session.add(notification)
        recipients.append((recipient, notification))
    return recipients


def queue_ticket_comment_notifications(ticket, comment):
    if not ticket or not comment or not comment.user or comment.is_internal or comment.deleted:
        return []

    from .models import TicketNotification, User, UserRole

    commenter_is_client = comment.user.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)
    if not commenter_is_client:
        return []

    recipients = (
        User.query.filter(
            User.is_active_user.is_(True),
            db.or_(
                User.role == UserRole.ADMIN,
                db.and_(
                    User.role == UserRole.ENGINEER,
                    db.func.lower(db.func.trim(User.user_type)) == "support",
                ),
            ),
        )
        .order_by(User.id.asc())
        .all()
    )

    preview = (comment.comment_text or "").strip() or "Attachment added"
    created_at = comment.created_at or datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    queued_recipients = []
    for recipient in recipients:
        if recipient.id == comment.user_id:
            continue
        notification = TicketNotification(
            ticket_id=ticket.id,
            task_id=None,
            recipient_id=recipient.id,
            actor_id=comment.user_id,
            notification_type="ticket_comment",
            comment_preview=preview,
            created_at=created_at,
        )
        db.session.add(notification)
        queued_recipients.append((recipient, notification))
    return queued_recipients


def queue_task_comment_notifications(task, actor=None):
    if not task or not actor:
        return []

    from .models import TicketNotification, UserRole

    actor_user_type = (getattr(actor, "user_type", "") or "").strip().lower()
    recipients = []

    actor_is_support_side = bool(
        actor.role == UserRole.ADMIN
        or actor_user_type == "support"
        or actor.id == task.reported_by_id
        or actor.id == task.assigned_by_id
    )

    if actor_is_support_side:
        if task.assigned_engineer_id and task.assigned_engineer_id != actor.id:
            recipients.append(task.assigned_engineer)
    else:
        if task.reported_by_id and task.reported_by_id != actor.id:
            recipients.append(task.reported_by)
        if task.assigned_by_id and task.assigned_by_id != actor.id:
            recipients.append(task.assigned_by)

    queued_recipients = []
    seen_recipient_ids = set()
    created_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    preview = "New comment from {}.".format(actor.full_name or actor.username or "System")

    for recipient in recipients:
        if (
            not recipient
            or not getattr(recipient, "is_active_user", False)
            or recipient.id in seen_recipient_ids
        ):
            continue
        seen_recipient_ids.add(recipient.id)
        notification = TicketNotification(
            ticket_id=None,
            task_id=task.id,
            recipient_id=recipient.id,
            actor_id=actor.id,
            notification_type="task_comment",
            comment_preview=preview,
            created_at=created_at,
        )
        db.session.add(notification)
        queued_recipients.append((recipient, notification))

    return queued_recipients


def queue_task_completed_notifications(task, actor=None):
    if not task or not actor:
        return []

    from .models import TicketNotification, UserRole

    actor_user_type = (getattr(actor, "user_type", "") or "").strip().lower()
    recipients = []

    actor_is_support_side = bool(
        actor.role == UserRole.ADMIN
        or actor_user_type == "support"
        or actor.id == task.reported_by_id
        or actor.id == task.assigned_by_id
    )

    if actor_is_support_side:
        if task.assigned_engineer_id and task.assigned_engineer_id != actor.id:
            recipients.append(task.assigned_engineer)
    else:
        if task.reported_by_id and task.reported_by_id != actor.id:
            recipients.append(task.reported_by)
        if task.assigned_by_id and task.assigned_by_id != actor.id:
            recipients.append(task.assigned_by)

    queued_recipients = []
    seen_recipient_ids = set()
    created_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    preview = "Task marked Fix/Completed by {}.".format(actor.full_name or actor.username or "System")

    for recipient in recipients:
        if (
            not recipient
            or not getattr(recipient, "is_active_user", False)
            or recipient.id in seen_recipient_ids
        ):
            continue
        seen_recipient_ids.add(recipient.id)
        notification = TicketNotification(
            ticket_id=None,
            task_id=task.id,
            recipient_id=recipient.id,
            actor_id=actor.id,
            notification_type="task_completed",
            comment_preview=preview,
            created_at=created_at,
        )
        db.session.add(notification)
        queued_recipients.append((recipient, notification))

    return queued_recipients


def mark_ticket_notifications_read_for_user(ticket_id: int, user) -> bool:
    if not ticket_id or not user:
        return False

    from .models import TicketNotification

    notifications = (
        TicketNotification.query
        .filter(
            TicketNotification.ticket_id == ticket_id,
            TicketNotification.recipient_id == user.id,
            TicketNotification.read_at.is_(None),
        )
        .all()
    )
    if not notifications:
        return False

    read_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    for notification in notifications:
        notification.read_at = read_at
    return True


def mark_task_notifications_read_for_user(task_id: int, user) -> bool:
    if not task_id or not user:
        return False

    from .models import TicketNotification

    notifications = (
        TicketNotification.query
        .filter(
            TicketNotification.task_id == task_id,
            TicketNotification.recipient_id == user.id,
            TicketNotification.notification_type.in_(("task_comment", "task_completed")),
            TicketNotification.read_at.is_(None),
        )
        .all()
    )
    if not notifications:
        return False

    read_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    for notification in notifications:
        notification.read_at = read_at
    return True


def mark_shared_ticket_comment_notifications_read(ticket_id: int) -> list:
    if not ticket_id:
        return []

    from .models import TicketNotification

    notifications = (
        TicketNotification.query
        .filter(
            TicketNotification.ticket_id == ticket_id,
            TicketNotification.notification_type == "ticket_comment",
            TicketNotification.read_at.is_(None),
        )
        .all()
    )
    if not notifications:
        return []

    read_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
    recipient_ids = []
    for notification in notifications:
        notification.read_at = read_at
        recipient_ids.append(notification.recipient_id)
    return sorted(set(recipient_ids))




def emit_header_notification_snapshot(user) -> None:
    if not user:
        return

    payload = build_header_notifications_for_user(user)
    socketio.emit(
        "ticket_comment_notification_count",
        {"count": payload.get("count", 0)},
        room=f"user_notifications:{user.id}",
    )
    socketio.emit(
        "ticket_comment_notification_snapshot",
        payload,
        room=f"user_notifications:{user.id}",
    )


def emit_header_notification_added(user, notification) -> None:
    if not user or not notification:
        return

    payload = header_notification_payload(notification)
    if not payload:
        emit_header_notification_snapshot(user)
        return

    socketio.emit(
        "ticket_comment_notification_added",
        {
            "count": build_header_notifications_for_user(user).get("count", 0),
            "notification": payload,
        },
        room=f"user_notifications:{user.id}",
    )


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    global migrate
    db.init_app(app)
    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    socketio.init_app(
        app,
        logger=app.config.get("SOCKETIO_LOGGER", False),
        engineio_logger=app.config.get("ENGINEIO_LOGGER", False),
        transports=app.config.get("SOCKETIO_SERVER_TRANSPORTS", ["websocket"]),
        ping_interval=app.config.get("SOCKETIO_PING_INTERVAL", 25),
        ping_timeout=app.config.get("SOCKETIO_PING_TIMEOUT", 20),
    )
    migrate = Migrate(app, db)
    app.jinja_env.filters["localtime"] = to_localtime
    app.jinja_env.filters["local_naive_to_localtime"] = local_naive_to_localtime
    app.jinja_env.filters["utc_naive_to_localtime"] = utc_naive_to_localtime
    app.jinja_env.globals["to_localtime"] = to_localtime
    app.jinja_env.globals["local_naive_to_localtime"] = local_naive_to_localtime
    app.jinja_env.globals["utc_naive_to_localtime"] = utc_naive_to_localtime
    app.jinja_env.globals["format_assigned_to_name"] = format_assigned_to_name
    app.jinja_env.globals["format_reported_by_name"] = format_reported_by_name

    from . import models  # noqa: F401
    from . import realtime  # noqa: F401

    @app.context_processor
    def inject_header_notifications():
        if not current_user.is_authenticated:
            return {
                "client_comment_notifications": [],
                "client_comment_notification_count": 0,
            }
        snapshot = build_header_notifications_for_user(current_user, limit=8)
        return {
            "client_comment_notifications": snapshot.get("notifications", []),
            "client_comment_notification_count": snapshot.get("count", 0),
        }

    from .auth import auth_bp
    from .main import main_bp
    from .tickets import tickets_bp
    from .instruments import instruments_bp
    from .service_logs import service_logs_bp
    from .uploads import uploads_bp
    from .admin import admin_bp
    from .profile import profile_bp
    from .lis_api import lis_api_bp
    from .reports import reports_bp
    from .pm_schedules import pm_schedules_bp
    from .weekly_schedules import weekly_schedules_bp
    from .monitoring import monitoring_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(main_bp)
    app.register_blueprint(tickets_bp, url_prefix="/tickets")
    app.register_blueprint(instruments_bp, url_prefix="/instruments")
    app.register_blueprint(service_logs_bp, url_prefix="/service-logs")
    app.register_blueprint(pm_schedules_bp, url_prefix="/pm-schedules")
    app.register_blueprint(weekly_schedules_bp, url_prefix="/weekly-schedules")
    app.register_blueprint(uploads_bp, url_prefix="/uploads")
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(profile_bp)
    app.register_blueprint(lis_api_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(monitoring_bp, url_prefix="/monitoring")

    return app
