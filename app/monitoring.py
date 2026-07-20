from datetime import datetime, timedelta

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from . import APP_TIMEZONE, db, to_localtime
from .models import Ticket, TicketStatus, TicketTask, UserRole

monitoring_bp = Blueprint("monitoring", __name__, template_folder="templates")

TASK_CATEGORY_PREFIX = "task:"
CLIENT_SCOPED_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN)
DISPLAY_TICKETS = "tickets"
DISPLAY_TICKET_TASK = "tickettask"


def _exclude_task_tickets(query):
    return query.filter(db.or_(Ticket.category.is_(None), ~Ticket.category.like(f"{TASK_CATEGORY_PREFIX}%")))


def _monitor_display_mode() -> str:
    requested_display = (request.args.get("display") or "").strip().lower()
    if requested_display == DISPLAY_TICKET_TASK:
        return DISPLAY_TICKET_TASK
    return DISPLAY_TICKETS


def _monitor_date_range():
    date_from_raw = (request.args.get("date_from") or "").strip()
    date_to_raw = (request.args.get("date_to") or "").strip()
    date_from = None
    date_to = None

    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
        except ValueError:
            date_from_raw = ""

    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d")
        except ValueError:
            date_to_raw = ""

    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
        date_from_raw = date_from.strftime("%Y-%m-%d")
        date_to_raw = date_to.strftime("%Y-%m-%d")

    return {
        "date_from": date_from,
        "date_to": date_to,
        "date_from_raw": date_from_raw,
        "date_to_raw": date_to_raw,
    }


def _scoped_query(model):
    query = model.query
    if current_user.role in CLIENT_SCOPED_ROLES:
        query = query.filter(model.client_id == current_user.client_id)
    return query


def _app_records(
    daily_only: bool = False,
    display: str = DISPLAY_TICKETS,
    date_from: datetime = None,
    date_to: datetime = None,
):
    model = TicketTask if display == DISPLAY_TICKET_TASK else Ticket
    query = _scoped_query(model)
    if model is Ticket:
        query = _exclude_task_tickets(query)
    query = query.filter(model.status != TicketStatus.CANCELLED)
    if date_from is not None:
        query = query.filter(model.created_at >= date_from)
    if date_to is not None:
        query = query.filter(model.created_at < (date_to + timedelta(days=1)))
    records = (
        query
        .filter(model.ticket_for == "app")
        .order_by(model.created_at.desc(), model.id.desc())
        .all()
    )
    if not daily_only:
        return records
    today_local = datetime.now(APP_TIMEZONE).date()
    return [record for record in records if record.created_at and to_localtime(record.created_at).date() == today_local]


def _app_monitoring_rows(
    daily_only: bool = False,
    display: str = DISPLAY_TICKETS,
    date_from: datetime = None,
    date_to: datetime = None,
):
    grouped = {}
    records = _app_records(
        daily_only=daily_only,
        display=display,
        date_from=date_from,
        date_to=date_to,
    )

    for ticket in records:
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
    rows.sort(key=lambda row: (-row["total"], (row["app_name"] or "").lower()))
    return rows


def _hospital_monitoring_rows(
    daily_only: bool = False,
    display: str = DISPLAY_TICKETS,
    date_from: datetime = None,
    date_to: datetime = None,
):
    grouped = {}
    records = _app_records(
        daily_only=daily_only,
        display=display,
        date_from=date_from,
        date_to=date_to,
    )

    for ticket in records:
        client_id = ticket.client_id or 0
        if client_id not in grouped:
            grouped[client_id] = {
                "client_id": ticket.client_id,
                "client_name": ticket.client.name if ticket.client else "Unassigned Hospital",
                "open": 0,
                "in_progress": 0,
                "resolved": 0,
                "closed": 0,
                "total": 0,
                "last_updated_at": ticket.updated_at or ticket.created_at,
            }

        row = grouped[client_id]
        row["last_updated_at"] = max(
            row["last_updated_at"] or datetime.min,
            ticket.updated_at or ticket.created_at or datetime.min,
        )

        if ticket.status == TicketStatus.OPEN:
            row["total"] += 1
            row["open"] += 1
        elif ticket.status == TicketStatus.IN_PROGRESS:
            row["total"] += 1
            row["in_progress"] += 1
        elif ticket.status == TicketStatus.RESOLVED:
            row["total"] += 1
            row["resolved"] += 1
        elif ticket.status == TicketStatus.CLOSED:
            row["closed"] += 1
        else:
            row["total"] += 1

    rows = list(grouped.values())
    rows.sort(key=lambda row: (-row["total"], (row["client_name"] or "").lower()))
    return rows


@monitoring_bp.before_request
def require_monitoring_access():
    if not current_user.is_authenticated:
        return
    endpoint = request.endpoint or ""
    if endpoint in ("monitoring.apps", "monitoring.apps_data"):
        if not current_user.has_nav_access("app_monitoring"):
            abort(403)
    elif endpoint in ("monitoring.apps_daily", "monitoring.apps_daily_data"):
        if not current_user.has_nav_access("daily_monitoring"):
            abort(403)


@monitoring_bp.route("/apps")
@login_required
def apps():
    display = _monitor_display_mode()
    date_range = _monitor_date_range()
    return render_template(
        "monitoring/apps.html",
        rows=_app_monitoring_rows(
            display=display,
            date_from=date_range["date_from"],
            date_to=date_range["date_to"],
        ),
        hospital_rows=_hospital_monitoring_rows(
            display=display,
            date_from=date_range["date_from"],
            date_to=date_range["date_to"],
        ),
        now=datetime.utcnow(),
        monitor_title="CDS Application Monitoring",
        monitor_subtitle="Realtime ticket status summary per CDS Application",
        monitor_data_url="monitoring.apps_data",
        monitor_display=display,
        monitor_display_label="Ticket Task" if display == DISPLAY_TICKET_TASK else "Tickets",
        monitor_date_from=date_range["date_from_raw"],
        monitor_date_to=date_range["date_to_raw"],
        enable_date_range=True,
    )


@monitoring_bp.route("/apps/data")
@login_required
def apps_data():
    display = _monitor_display_mode()
    date_range = _monitor_date_range()
    app_payload = []
    for row in _app_monitoring_rows(
        display=display,
        date_from=date_range["date_from"],
        date_to=date_range["date_to"],
    ):
        app_payload.append(
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
    hospital_payload = []
    for row in _hospital_monitoring_rows(
        display=display,
        date_from=date_range["date_from"],
        date_to=date_range["date_to"],
    ):
        hospital_payload.append(
            {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
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
    return jsonify(
        {
            "rows": app_payload,
            "hospital_rows": hospital_payload,
            "display": display,
            "date_from": date_range["date_from_raw"],
            "date_to": date_range["date_to_raw"],
        }
    )


@monitoring_bp.route("/apps-daily")
@login_required
def apps_daily():
    display = _monitor_display_mode()
    return render_template(
        "monitoring/apps.html",
        rows=_app_monitoring_rows(daily_only=True, display=display),
        hospital_rows=_hospital_monitoring_rows(daily_only=True, display=display),
        now=datetime.utcnow(),
        monitor_title="CDS Daily Monitoring",
        monitor_subtitle="",
        monitor_data_url="monitoring.apps_daily_data",
        monitor_display=display,
        monitor_display_label="Ticket Task" if display == DISPLAY_TICKET_TASK else "Tickets",
        monitor_date_from="",
        monitor_date_to="",
        enable_date_range=False,
    )


@monitoring_bp.route("/apps-daily/data")
@login_required
def apps_daily_data():
    display = _monitor_display_mode()
    app_payload = []
    for row in _app_monitoring_rows(daily_only=True, display=display):
        app_payload.append(
            {
                "app_id": row["app_id"],
                "app_name": row["app_name"],
                "open": row["open"],
                "in_progress": row["in_progress"],
                "resolved": row["resolved"],
                "closed": row["closed"],
                "total": row["total"],
            }
        )
    hospital_payload = []
    for row in _hospital_monitoring_rows(daily_only=True, display=display):
        hospital_payload.append(
            {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
                "total": row["total"],
            }
        )
    return jsonify({"rows": app_payload, "hospital_rows": hospital_payload, "display": display})
