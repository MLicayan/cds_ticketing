from datetime import datetime, timedelta, timezone

from flask import Flask
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
    app.jinja_env.globals["to_localtime"] = to_localtime

    from . import models  # noqa: F401
    from . import realtime  # noqa: F401

    @app.context_processor
    def inject_header_notifications():
        if not current_user.is_authenticated:
            return {
                "client_comment_notifications": [],
                "client_comment_notification_count": 0,
            }

        from .models import Ticket, TicketComment, User, UserRole

        change_prefixes = (
            "Status changed",
            "Priority changed",
            "Target schedule changed",
            "Date needed changed",
            "Complaint/details updated",
            "Engineer/IT assigned",
            "Engineer/IT assignment cleared",
            "Work status changed",
        )

        query = (
            db.session.query(TicketComment)
            .join(Ticket, TicketComment.ticket_id == Ticket.id)
            .join(User, TicketComment.user_id == User.id)
            .filter(TicketComment.is_internal.is_(False))
            .filter(Ticket.status != models.TicketStatus.CLOSED)
        )

        if current_user.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN):
            query = query.filter(~User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))
            query = query.filter(
                Ticket.client_id == current_user.client_id,
                Ticket.reported_by_id == current_user.id,
            )
        else:
            query = query.filter(User.role.in_([UserRole.CLIENT, UserRole.CLIENT_ADMIN]))

        current_user_type = (current_user.user_type or "").strip().lower()
        is_support_user = (
            current_user.role == UserRole.ENGINEER
            and current_user_type == "support"
        )

        if current_user.role == UserRole.ENGINEER and not is_support_user:
            query = query.filter(Ticket.assigned_engineer_id == current_user.id)
        elif current_user.role == UserRole.SALES:
            query = query.join(
                models.Client,
                Ticket.client_id == models.Client.id,
            ).filter(models.Client.assigned_sales_id == current_user.id)

        comments = query.order_by(TicketComment.created_at.desc()).limit(100).all()
        by_ticket = {}
        for comment in comments:
            if any((comment.comment_text or "").startswith(prefix) for prefix in change_prefixes):
                continue
            if comment.reaction_state_map().get(str(current_user.id), {}).get("acknowledge"):
                continue
            if comment.ticket_id not in by_ticket:
                by_ticket[comment.ticket_id] = {
                    "ticket": comment.ticket,
                    "latest_comment": comment,
                    "count": 0,
                }
            by_ticket[comment.ticket_id]["count"] += 1

        return {
            "client_comment_notifications": list(by_ticket.values())[:8],
            "client_comment_notification_count": len(by_ticket),
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
