import os

basedir = os.path.abspath(os.path.dirname(__file__))

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-me")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL",
        # "mysql+pymysql://cerebro_ticketing:Alp65230071@localhost:3306/cerebro_ticketing",
        "mysql+pymysql://root:6523007@localhost:3306/cerebro_ticketing"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER_TICKETS = os.path.join(basedir, "uploads", "tickets")
    UPLOAD_FOLDER_SERVICE_LOGS = os.path.join(basedir, "uploads", "service_logs")
    UPLOAD_FOLDER_PM_SCHEDULES = os.path.join(basedir, "uploads", "pm_schedules")
    MAX_CONTENT_LENGTH = 20 * 1024 * 1024  # 20 MB

    HOST = os.environ.get("FLASK_RUN_HOST", "0.0.0.0")
    PORT = int(os.environ.get("FLASK_RUN_PORT", 5013))
    DEBUG = os.environ.get("FLASK_DEBUG", "True").lower() in ("1", "true", "yes")
    USE_RELOADER = os.environ.get("FLASK_USE_RELOADER", "True").lower() in ("1", "true", "yes")