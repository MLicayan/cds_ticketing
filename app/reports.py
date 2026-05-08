from datetime import datetime
from sqlalchemy import func
from flask import Blueprint, render_template, abort
from flask_login import login_required, current_user

from . import db
from .models import (
    Instrument,
    ServiceLog,
    Ticket,
    User,
    UserRole,
    TicketStatus,
)


reports_bp = Blueprint("reports", __name__, template_folder="templates")
CLIENT_SCOPED_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN)


@reports_bp.before_request
def require_reports_nav_access():
    if current_user.is_authenticated and not current_user.has_nav_access("reports"):
        abort(403)


@reports_bp.route("/reports")
@login_required
def index():
    if current_user.role in CLIENT_SCOPED_ROLES and not current_user.client_id:
        abort(403)

    # Scoping for clients
    client_filter = []
    if current_user.role in CLIENT_SCOPED_ROLES:
        client_filter = [Ticket.client_id == current_user.client_id]

    top_instruments_visits = (
        db.session.query(Instrument, func.count(ServiceLog.id).label("visits"))
        .join(ServiceLog, ServiceLog.instrument_id == Instrument.id)
        .filter(*client_filter)
        .group_by(Instrument.id)
        .order_by(func.count(ServiceLog.id).desc())
        .limit(5)
        .all()
    )

    most_defective = (
        db.session.query(Instrument, func.count(Ticket.id).label("issues"))
        .join(Ticket, Ticket.instrument_id == Instrument.id)
        .filter(*client_filter)
        .group_by(Instrument.id)
        .order_by(func.count(Ticket.id).desc())
        .limit(5)
        .all()
    )

    top_engineers_services = (
        db.session.query(User, func.count(ServiceLog.id).label("logs"))
        .join(ServiceLog, ServiceLog.engineer_id == User.id)
        .filter(User.role == UserRole.ENGINEER, *client_filter)
        .group_by(User.id)
        .order_by(func.count(ServiceLog.id).desc())
        .limit(5)
        .all()
    )

    closed_statuses = [TicketStatus.RESOLVED, TicketStatus.CLOSED]
    closed_tickets = Ticket.query.filter(
        Ticket.status.in_(closed_statuses),
        Ticket.closed_at.isnot(None),
        *client_filter,
    ).all()
    if closed_tickets:
        deltas = [(t.closed_at - t.created_at).total_seconds() / 3600 for t in closed_tickets if t.created_at]
        avg_turnaround_hours = round(sum(deltas) / len(deltas), 2) if deltas else None
    else:
        avg_turnaround_hours = None

    open_like = [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.REOPENED]
    open_tickets = Ticket.query.filter(Ticket.status.in_(open_like), *client_filter).count()
    total_tickets = Ticket.query.filter(*client_filter).count()
    total_logs = ServiceLog.query.filter(*client_filter).count()

    return render_template(
        "reports/index.html",
        top_instruments_visits=top_instruments_visits,
        most_defective=most_defective,
        top_engineers_services=top_engineers_services,
        avg_turnaround_hours=avg_turnaround_hours,
        open_tickets=open_tickets,
        total_tickets=total_tickets,
        total_logs=total_logs,
    )
