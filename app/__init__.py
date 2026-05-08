from datetime import datetime, timedelta, timezone

from flask import Flask
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_socketio import SocketIO
from config import Config

db = SQLAlchemy()
login_manager = LoginManager()
socketio = SocketIO(cors_allowed_origins="*", async_mode="threading")
migrate = None
APP_TIMEZONE = timezone(timedelta(hours=8))


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
    socketio.init_app(app)
    migrate = Migrate(app, db)
    app.jinja_env.filters["localtime"] = to_localtime
    app.jinja_env.globals["to_localtime"] = to_localtime

    from . import models  # noqa: F401
    from . import realtime  # noqa: F401

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
