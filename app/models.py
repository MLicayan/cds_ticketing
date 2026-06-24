from datetime import datetime, timedelta, timezone
from enum import Enum
import base64
import hashlib
import hmac
import json

from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash

from . import db, login_manager

REACTION_SYMBOLS = {
    "acknowledge": "\U0001F44D",
    "thumbs_up": "\U0001F44D",
    "heart": "\u2764\ufe0f",
    "laugh": "\U0001F602",
    "wow": "\U0001F62E",
    "sad": "\U0001F622",
    "angry": "\U0001F621",
}

APP_TIMEZONE = timezone(timedelta(hours=8))


def _local_now_naive():
    return datetime.now(APP_TIMEZONE).replace(tzinfo=None)

class TicketStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"  # In-Process
    ON_HOLD = "on_hold"
    RESOLVED = "resolved"  # Fix/Completed
    REOPENED = "reopened"
    CLOSED = "closed"
    CANCELLED = "cancelled"


class TicketPriority(str, Enum):
    NOT_SET = "NOT_SET"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"
    
class MachineType(str, Enum):
    HEMATOLOGY = "HEMATOLOGY"
    CHEMISTRY = "CHEMISTRY"
    CLINICAL_MICROSCOPY = "CLINICAL_MICROSCOPY"
    IMMUNO_SEROLOGY = "IMMUNO_SEROLOGY"
    MICROBIOLOGY = "MICROBIOLOGY"
    BLOOD_BANKING = "BLOOD_BANKING"
    MOLECULAR_BIOLOGY = "MOLECULAR_BIOLOGY"
    OTHERS = "OTHERS"

class App(db.Model):
    __tablename__ = "apps"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)

    def __repr__(self):
        return f"<App {self.code}>"

class UserRole(str, Enum):
    ADMIN = "admin"
    ENGINEER = "engineer"
    SALES = "sales"
    CLIENT = "client"
    CLIENT_ADMIN = "client_admin"
    IT = "it"


NAV_ACCESS_OPTIONS = [
    ("tickets", "All Tickets"),
    ("my_tickets", "My Task"),
    ("developer_tasks", "Developer Tasks"),
    ("developer_workload", "Developer Workload"),
    ("service_logs", "Service Logs"),
    ("weekly_schedules", "Weekly Schedule"),
    ("pm_schedules", "PM Schedule"),
    ("instruments", "Instruments"),
    ("reports", "Reports"),
    ("app_monitoring", "App Monitoring"),
    ("daily_monitoring", "Daily Monitoring"),
]


class Client(db.Model):
    __tablename__ = "clients"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    client_code = db.Column(db.String(255))
    address = db.Column(db.String(255))
    contact_person = db.Column(db.String(255))
    contact_number = db.Column(db.String(50))
    assigned_sales_id = db.Column(db.Integer, nullable=True)
    email = db.Column(db.String(120))
    status = db.Column(db.String(20), default="active")

    instruments = db.relationship("Instrument", backref="client", lazy=True)
    users = db.relationship("User", backref="client", lazy=True)
    apps = db.relationship("App", secondary="client_apps", backref="clients", lazy="dynamic")

    def __repr__(self):
        return f"<Client {self.name}>"


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    full_name = db.Column(db.String(255))
    contact_number = db.Column(db.String(50))
    role = db.Column(db.Enum(UserRole), default=UserRole.CLIENT, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=True)
    is_active_user = db.Column(db.Boolean, default=True)
    user_type = db.Column(db.String(50), default="Engineer")  # Engineer or IT
    nav_permissions = db.Column(db.Text, nullable=True)

    tickets_reported = db.relationship(
        "Ticket",
        foreign_keys="Ticket.reported_by_id",
        backref="reported_by",
        lazy=True,
    )
    tickets_assigned = db.relationship(
        "Ticket",
        foreign_keys="Ticket.assigned_engineer_id",
        backref="assigned_engineer",
        lazy=True,
    )
    ticket_tasks_reported = db.relationship(
        "TicketTask",
        foreign_keys="TicketTask.reported_by_id",
        backref="reported_by",
        lazy=True,
    )
    ticket_tasks_assigned = db.relationship(
        "TicketTask",
        foreign_keys="TicketTask.assigned_engineer_id",
        backref="assigned_engineer",
        lazy=True,
    )

    def default_nav_permissions(self):
        role_value = self.role.value if self.role else ""
        if role_value == UserRole.ADMIN.value:
            return [key for key, _ in NAV_ACCESS_OPTIONS]
        if role_value == UserRole.ENGINEER.value:
            if (self.user_type or "").lower() == "it":
                return ["tickets", "my_tickets", "developer_workload", "service_logs"]
            return ["tickets", "my_tickets", "developer_workload", "service_logs", "weekly_schedules", "pm_schedules", "instruments", "reports"]
        if role_value == UserRole.SALES.value:
            return ["tickets", "my_tickets", "instruments"]
        if role_value == UserRole.CLIENT.value:
            return ["tickets", "my_tickets", "instruments"]
        if role_value == UserRole.CLIENT_ADMIN.value:
            return ["tickets", "my_tickets"]
        return []

    @property
    def is_client_scoped(self) -> bool:
        return self.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)

    @property
    def is_client_admin(self) -> bool:
        return self.role == UserRole.CLIENT_ADMIN

    def nav_permission_keys(self):
        if not self.nav_permissions:
            return self.default_nav_permissions()
        try:
            value = json.loads(self.nav_permissions)
        except (TypeError, ValueError):
            return self.default_nav_permissions()
        return value if isinstance(value, list) else self.default_nav_permissions()

    def set_nav_permissions(self, keys):
        allowed = {key for key, _ in NAV_ACCESS_OPTIONS}
        cleaned = [key for key in keys if key in allowed]
        self.nav_permissions = json.dumps(cleaned)

    def has_nav_access(self, key: str) -> bool:
        if self.role == UserRole.ADMIN and key == "admin":
            return True
        return key in self.nav_permission_keys()

    def set_password(self, password: str) -> None:
        # Explicitly use PBKDF2 for compatibility with older Werkzeug versions that
        # may not support newer defaults (e.g., scrypt).
        self.password_hash = generate_password_hash(password, method="pbkdf2:sha256")

    def check_password(self, password: str) -> bool:
        try:
            return check_password_hash(self.password_hash, password)
        except ValueError:
            # Fallback for legacy scrypt hashes generated on newer Werkzeug.
            if self.password_hash and self.password_hash.startswith("scrypt:"):
                return self._check_scrypt_password(password)
            raise

    def _check_scrypt_password(self, password: str) -> bool:
        """
        Manual scrypt verification for environments where hashlib doesn't recognize
        the encoded digest name used by Werkzeug (e.g., Python 3.6 with older OpenSSL).
        """
        try:
            method_part, salt, hashval = self.password_hash.split("$", 2)
        except ValueError:
            return False

        parts = method_part.split(":")
        if len(parts) != 4 or parts[0] != "scrypt":
            return False

        try:
            n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
            target_bytes = base64.b64decode(hashval)
            derived = hashlib.scrypt(
                password.encode("utf-8"),
                salt=salt.encode("utf-8"),
                n=n,
                r=r,
                p=p,
                dklen=len(target_bytes),
            )
            return hmac.compare_digest(derived, target_bytes)
        except Exception:
            return False

    def get_role_label(self) -> str:
        return self.role.value

    def __repr__(self):
        return f"<User {self.username}>"


@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


class Instrument(db.Model):
    __tablename__ = "instruments"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    model_id = db.Column(db.Integer, db.ForeignKey("instruments_model.id"), nullable=True)
    # brand = db.Column(db.String(255))
    # machine_type = db.Column(db.String(255), nullable=False)
    serial_number = db.Column(db.String(255))
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    installation_date = db.Column(db.Date)
    warranty_end_date = db.Column(db.Date)
    status = db.Column(db.String(20), default="active")
    notes = db.Column(db.Text)
    lis_status = db.Column(db.String(20), default="not_connected")  # connected, not_connected
    lis_protocol = db.Column(db.String(50))
    lis_last_active_at = db.Column(db.DateTime)
    lis_last_sent_at = db.Column(db.DateTime)
    lis_last_received_at = db.Column(db.DateTime)

    model = db.relationship("InstrumentModel", backref="instruments", lazy=True)
    tickets = db.relationship("Ticket", backref="instrument", lazy=True)
    service_logs = db.relationship("ServiceLog", backref="instrument", lazy=True)
    lis_logs = db.relationship("LISLog", backref="instrument", lazy=True)
    pm_schedules = db.relationship("PreventiveMaintenanceSchedule", backref="instrument", lazy=True)

    def display_label(self) -> str:
        # base = " - ".join(filter(None, [self.code, self.name]))
        base = " - ".join(filter(None, [self.name]))
        if self.serial_number:
            return f"{base} (SN: {self.serial_number})"
        return base

    def __repr__(self):
        return f"<Instrument {self.code}>"


class InstrumentModel(db.Model):
    __tablename__ = "instruments_model"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    brand_name = db.Column(db.String(255), nullable=False)
    machine_type = db.Column(db.Enum(MachineType), default=MachineType.OTHERS)

    def __repr__(self):
        return f"<InstrumentModel {self.code}>"
    
class ServiceType(db.Model):
    __tablename__ = "service_types"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(64), unique=True, nullable=False)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)

    def __repr__(self):
        return f"<ServiceType {self.code}>"


client_apps = db.Table(
    "client_apps",
    db.Column("client_id", db.Integer, db.ForeignKey("clients.id"), primary_key=True),
    db.Column("app_id", db.Integer, db.ForeignKey("apps.id"), primary_key=True),
)



class Ticket(db.Model):
    __tablename__ = "tickets"

    id = db.Column(db.Integer, primary_key=True)
    ticket_no = db.Column(db.String(32), unique=True, nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    instrument_id = db.Column(db.Integer, db.ForeignKey("instruments.id"), nullable=True)
    app_id = db.Column(db.Integer, db.ForeignKey("apps.id"), nullable=True)
    ticket_for = db.Column(db.String(20), default="instrument")  # instrument or app
    reported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_engineer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    category = db.Column(db.String(100))
    priority = db.Column(db.Enum(TicketPriority), default=TicketPriority.NOT_SET, nullable=False)
    subject = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.Enum(TicketStatus), default=TicketStatus.OPEN)
    kanban_bucket = db.Column(db.String(20), nullable=True)

    created_at = db.Column(db.DateTime, default=_local_now_naive)
    updated_at = db.Column(db.DateTime, default=_local_now_naive, onupdate=_local_now_naive)
    closed_at = db.Column(db.DateTime, nullable=True)
    
    started_date = db.Column(db.Date, default=datetime.utcnow,nullable=True) 
    is_working = db.Column(db.Boolean, default=False, nullable=True)
    
    date_needed = db.Column(db.DateTime, nullable=True) 
    

    target_date = db.Column(db.Date, nullable=True)
    
    comments = db.relationship("TicketComment", backref="ticket", lazy=True)
    service_logs = db.relationship("ServiceLog", backref="ticket", lazy=True)
    attachments = db.relationship("TicketAttachment", backref="ticket", lazy=True)

    client = db.relationship("Client")
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])
    app = db.relationship("App")

    def __repr__(self):
        return f"<Ticket {self.ticket_no}>"

    @property
    def signature_attachment(self):
        """Return the signature attachment if present."""
        if not self.attachments:
            return None
        for att in self.attachments:
            stored = (att.stored_filename or "").lower()
            original = (att.original_filename or "").lower()
            if "signature" in stored or "signature" in original:
                return att
        return None


class TicketNotification(db.Model):
    __tablename__ = "ticket_notifications"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=True)
    task_id = db.Column(db.Integer, db.ForeignKey("ticket_tasks.id"), nullable=True)
    recipient_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    actor_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    notification_type = db.Column(db.String(50), nullable=False, default="ticket_created")
    comment_preview = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=_local_now_naive, nullable=False)
    read_at = db.Column(db.DateTime, nullable=True)

    ticket = db.relationship("Ticket", foreign_keys=[ticket_id])
    task = db.relationship("TicketTask", foreign_keys=[task_id])
    recipient = db.relationship("User", foreign_keys=[recipient_id])
    actor = db.relationship("User", foreign_keys=[actor_id])

    def __repr__(self):
        return f"<TicketNotification {self.notification_type} ticket={self.ticket_id} task={self.task_id} recipient={self.recipient_id}>"


class DeveloperPrompt(db.Model):
    __tablename__ = "developer_prompts"

    id = db.Column(db.Integer, primary_key=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    title = db.Column(db.String(255), nullable=False)
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=_local_now_naive, nullable=False)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    responses = db.relationship(
        "DeveloperPromptResponse",
        backref="prompt",
        lazy=True,
        cascade="all, delete-orphan",
    )

    def response_counts(self):
        counts = {"pending": 0, "confirmed": 0, "denied": 0}
        for response in self.responses or []:
            key = (response.response_status or "pending").strip().lower()
            if key not in counts:
                key = "pending"
            counts[key] += 1
        return counts

    def __repr__(self):
        return f"<DeveloperPrompt {self.id} by {self.created_by_id}>"


class DeveloperPromptResponse(db.Model):
    __tablename__ = "developer_prompt_responses"

    id = db.Column(db.Integer, primary_key=True)
    prompt_id = db.Column(db.Integer, db.ForeignKey("developer_prompts.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    response_status = db.Column(db.String(20), nullable=False, default="pending")
    responded_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User", foreign_keys=[user_id])

    def __repr__(self):
        return f"<DeveloperPromptResponse prompt={self.prompt_id} user={self.user_id} status={self.response_status}>"


class TicketComment(db.Model):
    __tablename__ = "ticket_comments"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=True)
    parent_comment_id = db.Column(db.Integer, db.ForeignKey("ticket_comments.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_text = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    reactions_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")
    parent_comment = db.relationship(
        "TicketComment",
        remote_side=[id],
        backref=db.backref("replies", lazy=True),
    )

    def reaction_state_map(self):
        if not self.reactions_json:
            return {}
        try:
            data = json.loads(self.reactions_json)
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}

        normalized = {}
        for user_key, value in data.items():
            if isinstance(value, dict):
                reaction_code = value.get("reaction") or ""
                acknowledge = bool(value.get("acknowledge"))
            else:
                reaction_code = ""
                acknowledge = False
                if value == "acknowledge":
                    acknowledge = True
                elif isinstance(value, str) and value:
                    reaction_code = value

            if reaction_code or acknowledge:
                normalized[str(user_key)] = {
                    "reaction": reaction_code,
                    "acknowledge": acknowledge,
                }
        return normalized

    def reaction_map(self):
        return {
            user_key: state.get("reaction", "")
            for user_key, state in self.reaction_state_map().items()
            if state.get("reaction")
        }

    def reaction_summary(self):
        counts = {}
        for state in self.reaction_state_map().values():
            if state.get("acknowledge"):
                counts["acknowledge"] = counts.get("acknowledge", 0) + 1
            reaction_code = state.get("reaction") or ""
            if reaction_code:
                counts[reaction_code] = counts.get(reaction_code, 0) + 1
        return [
            {
                "code": reaction_code,
                "emoji": REACTION_SYMBOLS.get(reaction_code, reaction_code),
                "count": count,
            }
            for reaction_code, count in counts.items()
        ]

    @property
    def display_comment_text(self):
        if self.deleted:
            return "This comment was deleted."
        return self.comment_text or ""

    def __repr__(self):
        return f"<TicketComment {self.id}>"


class TicketTask(db.Model):
    __tablename__ = "ticket_tasks"

    id = db.Column(db.Integer, primary_key=True)
    task_no = db.Column(db.String(32), unique=True, nullable=False)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    instrument_id = db.Column(db.Integer, db.ForeignKey("instruments.id"), nullable=True)
    app_id = db.Column(db.Integer, db.ForeignKey("apps.id"), nullable=True)
    ticket_for = db.Column(db.String(20), default="instrument")
    reported_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    assigned_engineer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)

    priority = db.Column(
        db.Enum(TicketPriority, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=TicketPriority.NOT_SET,
        nullable=False,
    )
    subject = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(
        db.Enum(TicketStatus, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        default=TicketStatus.OPEN,
    )
    kanban_bucket = db.Column(db.String(20), nullable=True)

    created_at = db.Column(db.DateTime, default=_local_now_naive)
    updated_at = db.Column(db.DateTime, default=_local_now_naive, onupdate=_local_now_naive)
    closed_at = db.Column(db.DateTime, nullable=True)
    started_date = db.Column(db.Date, default=datetime.utcnow, nullable=True)
    is_working = db.Column(db.Boolean, default=False, nullable=True)
    date_needed = db.Column(db.DateTime, nullable=True)
    target_date = db.Column(db.Date, nullable=True)

    parent_ticket = db.relationship("Ticket", backref=db.backref("tasks", lazy=True))
    client = db.relationship("Client")
    instrument = db.relationship("Instrument")
    app = db.relationship("App")
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])
    comments = db.relationship("TicketTaskComment", backref="task", lazy=True, cascade="all, delete-orphan")
    attachments = db.relationship("TicketTaskAttachment", backref="task", lazy=True, cascade="all, delete-orphan")
    work_sessions = db.relationship("TicketTaskWorkSession", backref="task", lazy=True, cascade="all, delete-orphan")

    @property
    def ticket_no(self):
        return self.task_no

    @ticket_no.setter
    def ticket_no(self, value):
        self.task_no = value

    @property
    def service_logs(self):
        return []

    @property
    def signature_attachment(self):
        if not self.attachments:
            return None
        for att in self.attachments:
            stored = (att.stored_filename or "").lower()
            original = (att.original_filename or "").lower()
            if "signature" in stored or "signature" in original:
                return att
        return None

    def __repr__(self):
        return f"<TicketTask {self.task_no}>"


class TicketTaskWorkSession(db.Model):
    __tablename__ = "ticket_task_work_sessions"

    id = db.Column(db.Integer, primary_key=True)
    ticket_task_id = db.Column(db.Integer, db.ForeignKey("ticket_tasks.id"), nullable=False)
    developer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    started_at = db.Column(db.DateTime, nullable=False, default=_local_now_naive)
    paused_at = db.Column(db.DateTime, nullable=True)
    ended_at = db.Column(db.DateTime, nullable=True)
    pause_reason = db.Column(db.String(255), nullable=True)
    pause_type = db.Column(db.String(50), nullable=True)
    created_at = db.Column(db.DateTime, default=_local_now_naive, nullable=False)
    updated_at = db.Column(db.DateTime, default=_local_now_naive, onupdate=_local_now_naive)

    developer = db.relationship("User")

    def __repr__(self):
        return f"<TicketTaskWorkSession {self.id}>"


class TicketTaskComment(db.Model):
    __tablename__ = "ticket_task_comments"

    id = db.Column(db.Integer, primary_key=True)
    ticket_task_id = db.Column(db.Integer, db.ForeignKey("ticket_tasks.id"), nullable=False)
    parent_comment_id = db.Column(db.Integer, db.ForeignKey("ticket_task_comments.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_text = db.Column(db.Text, nullable=False)
    is_internal = db.Column(db.Boolean, default=False)
    deleted = db.Column(db.Boolean, default=False, nullable=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    reactions_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")
    parent_comment = db.relationship(
        "TicketTaskComment",
        remote_side=[id],
        backref=db.backref("replies", lazy=True),
    )

    def reaction_state_map(self):
        return TicketComment.reaction_state_map(self)

    def reaction_map(self):
        return TicketComment.reaction_map(self)

    def reaction_summary(self):
        return TicketComment.reaction_summary(self)

    @property
    def display_comment_text(self):
        return TicketComment.display_comment_text.fget(self)

    def __repr__(self):
        return f"<TicketTaskComment {self.id}>"


class ServiceLog(db.Model):
    __tablename__ = "service_logs"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=True)
    pm_schedule_id = db.Column(db.Integer, db.ForeignKey("preventive_maintenance_schedules.id"), nullable=True)
    instrument_id = db.Column(db.Integer, db.ForeignKey("instruments.id"), nullable=True)
    app_id = db.Column(db.Integer, db.ForeignKey("apps.id"), nullable=True)
    service_for = db.Column(db.String(20), default="instrument", nullable=True)  # instrument or app
    
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    engineer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    service_type = db.Column(db.String(50))  # corrective, pm, install, calibration, remote
    visit_date = db.Column(db.Date, default=datetime.utcnow)
    start_time = db.Column(db.Time, nullable=True)
    end_time = db.Column(db.Time, nullable=True)

    problem_description = db.Column(db.Text)
    root_cause = db.Column(db.Text)
    action_taken = db.Column(db.Text)
    parts_used = db.Column(db.Text)
    recommendations = db.Column(db.Text)
    status_after  = db.Column(db.String(50))  # operational, non-operational
    is_monitor = db.Column(db.Boolean, default=False)
    monitored_days  = db.Column(db.Integer, nullable=True, default=0)  

    confirmed_by = db.Column(db.String(255))
    confirmed_by_position = db.Column(db.String(255))
    confirm_photo_name = db.Column(db.String(255))

    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    engineer = db.relationship("User")
    client = db.relationship("Client")
    pm_schedule = db.relationship("PreventiveMaintenanceSchedule", backref="service_logs")

    attachments = db.relationship("ServiceLogAttachment", backref="service_log", lazy=True)
    service_log_parts = db.relationship(
        "ServiceLogPart",
        backref="service_log",
        lazy=True,
        cascade="all, delete-orphan",
    )
    parts = db.relationship("Part", secondary="service_log_parts", viewonly=True, lazy="subquery")
    app = db.relationship("App")

    def __repr__(self):
        return f"<ServiceLog {self.id}>"

    @property
    def signature_attachment(self):
        """Return the signature attachment if present."""
        if not self.attachments:
            return None
        for att in self.attachments:
            stored = (att.stored_filename or "").lower()
            original = (att.original_filename or "").lower()
            if "signature" in stored or "signature" in original:
                return att
        return None


class WeeklySchedule(db.Model):
    __tablename__ = "weekly_schedules"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(255))
    week_start = db.Column(db.Date, nullable=False)
    week_end = db.Column(db.Date, nullable=False)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    created_by = db.relationship("User")
    tasks = db.relationship(
        "WeeklyScheduleTask",
        backref="schedule",
        lazy=True,
        cascade="all, delete-orphan",
    )


class WeeklyScheduleTask(db.Model):
    __tablename__ = "weekly_schedule_tasks"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("weekly_schedules.id"), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    instrument_id = db.Column(db.Integer, db.ForeignKey("instruments.id"), nullable=True)
    app_id = db.Column(db.Integer, db.ForeignKey("apps.id"), nullable=True)
    ticket_for = db.Column(db.String(20), default="instrument")  # instrument or app
    engineer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    pm_schedule_id = db.Column(db.Integer, db.ForeignKey("preventive_maintenance_schedules.id"), nullable=True)
    service_type = db.Column(db.String(50))
    subject = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)
    priority = db.Column(db.Enum(TicketPriority), default=TicketPriority.MEDIUM)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    client = db.relationship("Client")
    instrument = db.relationship("Instrument")
    app = db.relationship("App")
    engineer = db.relationship("User", foreign_keys=[engineer_id])
    ticket = db.relationship("Ticket")
    pm_schedule = db.relationship("PreventiveMaintenanceSchedule")


class PreventiveMaintenanceSchedule(db.Model):
    __tablename__ = "preventive_maintenance_schedules"

    id = db.Column(db.Integer, primary_key=True)
    doc_no = db.Column(db.String(32), unique=True, nullable=False)
    description = db.Column(db.Text)
    client_id = db.Column(db.Integer, db.ForeignKey("clients.id"), nullable=False)
    instrument_id = db.Column(db.Integer, db.ForeignKey("instruments.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    task_duration = db.Column(db.String(50))
    assigned_engineer_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    assigned_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    client = db.relationship("Client")
    assigned_engineer = db.relationship("User", foreign_keys=[assigned_engineer_id])
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_id])
    ticket = db.relationship("Ticket")
    comments = db.relationship("PMScheduleComment", backref="schedule", lazy=True, cascade="all, delete-orphan")
    attachments = db.relationship("PMScheduleAttachment", backref="schedule", lazy=True, cascade="all, delete-orphan")

    def __repr__(self):
        return f"<PM {self.doc_no}>"


class PMScheduleComment(db.Model):
    __tablename__ = "pm_schedule_comments"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("preventive_maintenance_schedules.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    comment_text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<PMScheduleComment {self.id}>"


class PMScheduleAttachment(db.Model):
    __tablename__ = "pm_schedule_attachments"

    id = db.Column(db.Integer, primary_key=True)
    schedule_id = db.Column(db.Integer, db.ForeignKey("preventive_maintenance_schedules.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(128))
    file_size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<PMScheduleAttachment {self.id}>"


class TicketAttachment(db.Model):
    __tablename__ = "ticket_attachments"

    id = db.Column(db.Integer, primary_key=True)
    ticket_id = db.Column(db.Integer, db.ForeignKey("tickets.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(128))
    file_size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<TicketAttachment {self.id}>"


class TicketTaskAttachment(db.Model):
    __tablename__ = "ticket_task_attachments"

    id = db.Column(db.Integer, primary_key=True)
    ticket_task_id = db.Column(db.Integer, db.ForeignKey("ticket_tasks.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(128))
    file_size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<TicketTaskAttachment {self.id}>"


class ServiceLogAttachment(db.Model):
    __tablename__ = "service_log_attachments"

    id = db.Column(db.Integer, primary_key=True)
    service_log_id = db.Column(db.Integer, db.ForeignKey("service_logs.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    stored_filename = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(128))
    file_size = db.Column(db.Integer)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")

    def __repr__(self):
        return f"<ServiceLogAttachment {self.id}>"


class ServiceLogPart(db.Model):
    __tablename__ = "service_log_parts"

    id = db.Column(db.Integer, primary_key=True)
    service_log_id = db.Column(db.Integer, db.ForeignKey("service_logs.id"), nullable=False)
    part_id = db.Column(db.Integer, db.ForeignKey("parts.id"), nullable=True)
    part_no = db.Column(db.String(255))
    qty = db.Column(db.Numeric(12, 2))
    price = db.Column(db.Numeric(12, 2))
    total = db.Column(db.Numeric(12, 2))
    under_warranty = db.Column(db.Boolean, default=False)

    part = db.relationship("Part")


class Part(db.Model):
    __tablename__ = "parts"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), unique=True, nullable=False)
    description = db.Column(db.Text)
    unit_cost = db.Column(db.Numeric(12, 2))
    price = db.Column(db.Numeric(12, 2))

    def __repr__(self):
        return f"<Part {self.name}>"


class LISLog(db.Model):
    __tablename__ = "lis_logs"

    id = db.Column(db.Integer, primary_key=True)
    instrument_id = db.Column(db.Integer, db.ForeignKey("instruments.id"), nullable=False)
    direction = db.Column(db.String(10))  # rx, tx
    message = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<LISLog {self.id}>"
