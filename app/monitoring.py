from datetime import datetime

from flask import Blueprint, abort, jsonify, render_template, request
from flask_login import current_user, login_required

from . import APP_TIMEZONE, db, to_localtime
from .models import Ticket, TicketStatus, UserRole

monitoring_bp = Blueprint("monitoring", __name__, template_folder="templates")

TASK_CATEGORY_PREFIX = "task:"
CLIENT_SCOPED_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN)


def _exclude_task_tickets(query):
    return query.filter(db.or_(Ticket.category.is_(None), ~Ticket.category.like(f"{TASK_CATEGORY_PREFIX}%")))


def _scoped_ticket_query():
    query = _exclude_task_tickets(Ticket.query)
    if current_user.role in CLIENT_SCOPED_ROLES:
        query = query.filter(Ticket.client_id == current_user.client_id)
    elif current_user.role == UserRole.ENGINEER:
        query = query.filter(Ticket.assigned_engineer_id == current_user.id)
    return query


def _app_tickets(daily_only: bool = False):
    tickets = (
        _scoped_ticket_query()
        .filter(Ticket.ticket_for == "app")
        .order_by(Ticket.created_at.desc(), Ticket.id.desc())
        .all()
    )
    if not daily_only:
        return tickets
    today_local = datetime.now(APP_TIMEZONE).date()
    return [ticket for ticket in tickets if ticket.created_at and to_localtime(ticket.created_at).date() == today_local]


def _app_monitoring_rows(daily_only: bool = False):
    grouped = {}
    tickets = _app_tickets(daily_only=daily_only)

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
    rows.sort(key=lambda row: (-row["total"], (row["app_name"] or "").lower()))
    return rows


def _hospital_monitoring_rows(daily_only: bool = False):
    grouped = {}
    tickets = _app_tickets(daily_only=daily_only)

    for ticket in tickets:
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
    rows.sort(key=lambda row: (-row["total"], (row["client_name"] or "").lower()))
    return rows


@monitoring_bp.before_request
def require_monitoring_access():
    if not current_user.is_authenticated:
        return
    if current_user.role != UserRole.ADMIN:
        abort(403)
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
    return render_template(
        "monitoring/apps.html",
        rows=_app_monitoring_rows(),
        hospital_rows=_hospital_monitoring_rows(),
        now=datetime.utcnow(),
        monitor_title="CDS Application Monitoring",
        monitor_subtitle="Realtime ticket status summary per CDS Application",
        monitor_data_url="monitoring.apps_data",
    )


@monitoring_bp.route("/apps/data")
@login_required
def apps_data():
    app_payload = []
    for row in _app_monitoring_rows():
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
    for row in _hospital_monitoring_rows():
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
    return jsonify({"rows": app_payload, "hospital_rows": hospital_payload})


@monitoring_bp.route("/apps-daily")
@login_required
def apps_daily():
    return render_template(
        "monitoring/apps.html",
        rows=_app_monitoring_rows(daily_only=True),
        hospital_rows=_hospital_monitoring_rows(daily_only=True),
        now=datetime.utcnow(),
        monitor_title="CDS Daily Monitoring",
        monitor_subtitle="Tickets created today from 12:00 AM to 11:59 PM",
        monitor_data_url="monitoring.apps_daily_data",
    )


@monitoring_bp.route("/apps-daily/data")
@login_required
def apps_daily_data():
    app_payload = []
    for row in _app_monitoring_rows(daily_only=True):
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
    for row in _hospital_monitoring_rows(daily_only=True):
        hospital_payload.append(
            {
                "client_id": row["client_id"],
                "client_name": row["client_name"],
                "total": row["total"],
            }
        )
    return jsonify({"rows": app_payload, "hospital_rows": hospital_payload})
