import os

basedir = os.path.abspath(os.path.dirname(__file__))


def env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default):
    value = os.environ.get(name)
    if value is None:
        return default
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or default


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        "mysql+pymysql://cerebro_ticketing:Alp65230071@localhost:3306/cerebro_ticketing",
        # "mysql+pymysql://root:6523007@localhost:3306/cerebro_ticketing",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER_TICKETS = os.path.join(basedir, "uploads", "tickets")
    UPLOAD_FOLDER_SERVICE_LOGS = os.path.join(basedir, "uploads", "service_logs")
    UPLOAD_FOLDER_PM_SCHEDULES = os.path.join(basedir, "uploads", "pm_schedules")
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB

    HOST = os.environ.get("FLASK_RUN_HOST", "0.0.0.0")
    PORT = int(os.environ.get("FLASK_RUN_PORT", 5013))
    DEBUG = env_bool("FLASK_DEBUG", False)
    USE_RELOADER = env_bool("FLASK_USE_RELOADER", False)
    SOCKETIO_LOGGER = env_bool("SOCKETIO_LOGGER", DEBUG)
    ENGINEIO_LOGGER = env_bool("ENGINEIO_LOGGER", DEBUG)
    SOCKETIO_SERVER_TRANSPORTS = env_list("SOCKETIO_SERVER_TRANSPORTS", ["polling", "websocket"])
    SOCKETIO_CLIENT_TRANSPORTS = env_list("SOCKETIO_CLIENT_TRANSPORTS", ["polling"])
    SOCKETIO_CLIENT_UPGRADE = env_bool("SOCKETIO_CLIENT_UPGRADE", False)
    SOCKETIO_PING_INTERVAL = int(os.environ.get("SOCKETIO_PING_INTERVAL", 25))
    SOCKETIO_PING_TIMEOUT = int(os.environ.get("SOCKETIO_PING_TIMEOUT", 20))
