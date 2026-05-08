import re
from flask import Blueprint, render_template, abort, request
from flask_login import login_required, current_user
from datetime import datetime, timedelta

from .models import (
    Instrument,
    InstrumentModel,
    UserRole,
    ServiceLog,
    Ticket,
    TicketStatus,
    LISLog,
    Client,
    PreventiveMaintenanceSchedule,
)

instruments_bp = Blueprint("instruments", __name__, template_folder="templates")
CLIENT_SCOPED_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN)


@instruments_bp.before_request
def require_instruments_nav_access():
    if current_user.is_authenticated and current_user.role == UserRole.CLIENT_ADMIN:
        abort(403)
    if current_user.is_authenticated and not current_user.has_nav_access("instruments"):
        abort(403)


def _slugify_identifier(value: str) -> str:
    """Normalize identifiers like CODE or NAME-SERIALNO into slug-ish strings."""
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value or "")
    return normalized.strip("-").lower()


def _get_instrument_by_identifier(identifier: str) -> Instrument:
    """
    Resolve an instrument by code or a name-serial slug, respecting client scoping.
    - Exact/ilike match on code
    - Fallback to slugified `<name>-<serial_number>`
    """
    query = Instrument.query
    if current_user.is_authenticated and getattr(current_user, "role", None) in CLIENT_SCOPED_ROLES:
        query = query.filter(Instrument.client_id == current_user.client_id)

    instrument = query.filter(Instrument.code.ilike(identifier)).first()
    if instrument:
        return instrument

    normalized = _slugify_identifier(identifier)
    for ins in query.all():
        candidates = {_slugify_identifier(ins.code)}
        name_serial = "-".join(filter(None, [ins.name, ins.serial_number]))
        if name_serial:
            candidates.add(_slugify_identifier(name_serial))
        if normalized in candidates:
            return ins

    abort(404)


@instruments_bp.route("/")
@login_required
def index():
    name_q = request.args.get("name") or ""
    serial_q = request.args.get("serial_number") or ""
    model_q = request.args.get("model") or ""
    client_id = request.args.get("client_id") or ""

    query = Instrument.query
    clients = Client.query.order_by(Client.name.asc()).all()

    if current_user.role in CLIENT_SCOPED_ROLES:
        query = query.filter(Instrument.client_id == current_user.client_id)
        clients = [c for c in clients if c.id == current_user.client_id]
    else:
        if client_id:
            try:
                query = query.filter(Instrument.client_id == int(client_id))
            except ValueError:
                pass

    if name_q:
        query = query.filter(Instrument.name.ilike(f"%{name_q}%"))
    if serial_q:
        query = query.filter(Instrument.serial_number.ilike(f"%{serial_q}%"))
    if model_q:
        query = query.join(InstrumentModel, isouter=True).filter(
            (InstrumentModel.name.ilike(f"%{model_q}%")) | (InstrumentModel.code.ilike(f"%{model_q}%"))
        )

    instruments = query.order_by(Instrument.name.asc()).all()
    return render_template(
        "instruments/index.html",
        instruments=instruments,
        clients=clients,
        selected_filters={
            "name": name_q,
            "serial_number": serial_q,
            "model": model_q,
            "client_id": client_id,
        },
    )


@instruments_bp.route("/<identifier>/logs")
@login_required
def logs(identifier):
    instrument = _get_instrument_by_identifier(identifier)
    logs = (
        ServiceLog.query.filter(ServiceLog.instrument_id == instrument.id)
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .all()
    )
    
    pm_logs = (
        PreventiveMaintenanceSchedule.query.filter(PreventiveMaintenanceSchedule.instrument_id == instrument.id)
        .order_by(PreventiveMaintenanceSchedule.date.desc(), PreventiveMaintenanceSchedule.id.desc())
        .all()
    )

    last_pm = (
        ServiceLog.query.filter(
            ServiceLog.instrument_id == instrument.id,
            ServiceLog.service_type.in_(["pm", "pm-onsite", "pm-remote"]),
        )
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .first()
    )
    last_calibration = (
        ServiceLog.query.filter(
            ServiceLog.instrument_id == instrument.id,
            ServiceLog.service_type.in_(["calibration", "calibration-onsite", "calibration-remote"]),
        )
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .first()
    )
    last_install = (
        ServiceLog.query.filter(
            ServiceLog.instrument_id == instrument.id,
            ServiceLog.service_type.in_(["install", "install-onsite", "install-remote"]),
        )
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .first()
    )
    
    code_slug = _slugify_identifier(instrument.code)
    name_serial_slug = _slugify_identifier("-".join(filter(None, [instrument.name, instrument.serial_number])))
    return render_template(
        "instruments/logs.html",
        instrument=instrument,
        logs=logs,
        pm_logs=pm_logs,
        last_install=last_install,
        last_pm=last_pm,
        last_calibration=last_calibration,
        code_slug=code_slug,
        name_serial_slug=name_serial_slug if name_serial_slug and name_serial_slug != code_slug else None,
    )


@instruments_bp.route("/<int:instrument_id>")
@login_required
def detail(instrument_id):
    instrument = Instrument.query.get_or_404(instrument_id)
    if current_user.role in CLIENT_SCOPED_ROLES and instrument.client_id != current_user.client_id:
        abort(403)

    open_like_statuses = [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.REOPENED]

    last_pm = (
        ServiceLog.query.filter(
            ServiceLog.instrument_id == instrument.id,
            ServiceLog.service_type == "pm",
        )
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .first()
    )
    last_calibration = (
        ServiceLog.query.filter(
            ServiceLog.instrument_id == instrument.id,
            ServiceLog.service_type == "calibration",
        )
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .first()
    )
    last_install = (
        ServiceLog.query.filter(
            ServiceLog.instrument_id == instrument.id,
            ServiceLog.service_type == "install",
        )
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .first()
    )

    latest_open_ticket = (
        Ticket.query.filter(
            Ticket.instrument_id == instrument.id,
            Ticket.status.in_(open_like_statuses),
        )
        .order_by(Ticket.created_at.desc())
        .first()
    )

    total_tickets = Ticket.query.filter(Ticket.instrument_id == instrument.id).count()
    total_pending_tickets = (
        Ticket.query.filter(Ticket.instrument_id == instrument.id, Ticket.status.in_(open_like_statuses)).count()
    )
    total_logs = ServiceLog.query.filter(ServiceLog.instrument_id == instrument.id).count()

    recent_logs = (
        ServiceLog.query.filter(ServiceLog.instrument_id == instrument.id)
        .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
        .limit(10)
        .all()
    )

    lis_logs = (
        LISLog.query.filter(LISLog.instrument_id == instrument.id)
        .order_by(LISLog.created_at.desc(), LISLog.id.desc())
        .limit(10)
        .all()
    )

    # Timeline data (last 12 weeks, weekly buckets)
    today = datetime.utcnow().date()
    week_start = today - timedelta(days=today.weekday())  # align to Monday
    start_date = week_start - timedelta(weeks=11)
    ticket_history = Ticket.query.filter(
        Ticket.instrument_id == instrument.id,
        Ticket.created_at >= datetime.combine(start_date, datetime.min.time()),
    ).all()
    service_history = ServiceLog.query.filter(
        ServiceLog.instrument_id == instrument.id,
        ServiceLog.created_at >= datetime.combine(start_date, datetime.min.time()),
    ).all()

    def build_week_counts(items, date_attr):
        counts = {}
        for itm in items:
            dt = getattr(itm, date_attr)
            if not dt:
                continue
            d = dt.date()
            monday = d - timedelta(days=d.weekday())
            # key = f"{monday.isocalendar().year}-W{monday.isocalendar().week:02d}"
            # counts[key] = counts.get(key, 0) + 1
            iso_year, iso_week, _ = monday.isocalendar()
            key = f"{iso_year}-W{iso_week:02d}"
            counts[key] = counts.get(key, 0) + 1
        return counts

    ticket_counts_map = build_week_counts(ticket_history, "created_at")
    log_counts_map = build_week_counts(service_history, "created_at")

    # Generate ordered labels for last 12 weeks
    labels = []
    t_counts = []
    l_counts = []
    
    for i in range(11, -1, -1):
        week_monday = week_start - timedelta(weeks=i)
        iso_year, iso_week, _ = week_monday.isocalendar()
        key = f"{iso_year}-W{iso_week:02d}"
        labels.append(week_monday.strftime("%b%d"))
        t_counts.append(ticket_counts_map.get(key, 0))
        l_counts.append(log_counts_map.get(key, 0))

    # for i in range(11, -1, -1):
    #     week_monday = week_start - timedelta(weeks=i)
    #     key = f"{week_monday.isocalendar().year}-W{week_monday.isocalendar().week:02d}"
    #     labels.append(week_monday.strftime("%b%d"))
    #     t_counts.append(ticket_counts_map.get(key, 0))
    #     l_counts.append(log_counts_map.get(key, 0))

    return render_template(
        "instruments/detail.html",
        instrument=instrument,
        last_pm=last_pm,
        last_calibration=last_calibration,
        last_install=last_install,
        latest_open_ticket=latest_open_ticket,
        total_tickets=total_tickets,
        total_pending_tickets=total_pending_tickets,
        total_logs=total_logs,
        recent_logs=recent_logs,
        lis_logs=lis_logs,
        timeline_labels=labels,
        timeline_ticket_counts=t_counts,
        timeline_log_counts=l_counts,
    )


@instruments_bp.route("/<int:instrument_id>/qr")
# @login_required
def qr_preview(instrument_id):
    instrument = Instrument.query.get_or_404(instrument_id)
    # if current_user.role == UserRole.CLIENT and instrument.client_id != current_user.client_id:
    #     abort(403)
    return render_template("qrcode_instrument.html", instrument=instrument)
