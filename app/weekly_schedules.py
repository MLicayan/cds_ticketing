from datetime import datetime, timedelta

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from . import db
from .models import (
    WeeklySchedule,
    WeeklyScheduleTask,
    Client,
    Instrument,
    App,
    User,
    UserRole,
    Ticket,
    TicketStatus,
    TicketPriority,
    ServiceLog,
    PreventiveMaintenanceSchedule,
)


weekly_schedules_bp = Blueprint("weekly_schedules", __name__, template_folder="templates")


def generate_ticket_no(ticket_id: int) -> str:
    return f"T-{ticket_id:06d}"


def require_engineer_or_admin():
    if not current_user.has_nav_access("weekly_schedules"):
        abort(403)


@weekly_schedules_bp.route("/")
@login_required
def index():
    require_engineer_or_admin()
    schedules = WeeklySchedule.query.order_by(WeeklySchedule.week_start.desc()).all()
    return render_template("weekly_schedules_index.html", schedules=schedules)


@weekly_schedules_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    require_engineer_or_admin()

    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    apps = App.query.order_by(App.name.asc()).all()
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    pm_schedules = (
        PreventiveMaintenanceSchedule.query.filter(~PreventiveMaintenanceSchedule.service_logs.any())
        .order_by(PreventiveMaintenanceSchedule.date.asc())
        .all()
    )

    today = datetime.utcnow().date()
    default_week_start = today - timedelta(days=today.weekday())  # Monday

    if request.method == "POST":
        title = (request.form.get("title") or "").strip()
        week_start_raw = request.form.get("week_start") or default_week_start.strftime("%Y-%m-%d")
        try:
            week_start = datetime.strptime(week_start_raw, "%Y-%m-%d").date()
        except ValueError:
            week_start = default_week_start
        week_end = week_start + timedelta(days=6)

        task_subjects = request.form.getlist("task_subjects[]")
        task_descriptions = request.form.getlist("task_descriptions[]")
        task_client_ids = request.form.getlist("task_client_ids[]")
        task_instrument_ids = request.form.getlist("task_instrument_ids[]")
        task_app_ids = request.form.getlist("task_app_ids[]")
        task_ticket_for = request.form.getlist("task_ticket_for[]")
        task_engineer_ids = request.form.getlist("task_engineer_ids[]")
        task_service_types = request.form.getlist("task_service_types[]")
        task_pm_schedule_ids = request.form.getlist("task_pm_schedule_ids[]")
        task_priorities = request.form.getlist("task_priorities[]")

        tasks_payload = []
        max_rows = max(
            len(task_subjects),
            len(task_descriptions),
            len(task_client_ids),
            len(task_instrument_ids),
            len(task_engineer_ids),
            len(task_service_types),
            len(task_priorities),
        )

        for idx in range(max_rows):
            subject = (task_subjects[idx] if idx < len(task_subjects) else "").strip()
            description = (task_descriptions[idx] if idx < len(task_descriptions) else "").strip()
            client_id = task_client_ids[idx] if idx < len(task_client_ids) else ""
            instrument_id = task_instrument_ids[idx] if idx < len(task_instrument_ids) else ""
            app_id = task_app_ids[idx] if idx < len(task_app_ids) else ""
            ticket_for = (task_ticket_for[idx] if idx < len(task_ticket_for) else "") or "instrument"
            engineer_id = task_engineer_ids[idx] if idx < len(task_engineer_ids) else ""
            service_type = (task_service_types[idx] if idx < len(task_service_types) else "") or "pm-onsite"
            priority_raw = (task_priorities[idx] if idx < len(task_priorities) else "") or TicketPriority.MEDIUM.value
            pm_schedule_id_raw = task_pm_schedule_ids[idx] if idx < len(task_pm_schedule_ids) else ""

            if not subject or not client_id or (ticket_for == "instrument" and not instrument_id) or (ticket_for == "app" and not app_id):
                continue
            if service_type == "pm-onsite" and not instrument_id:
                continue

            try:
                priority_val = TicketPriority(priority_raw)
            except ValueError:
                priority_val = TicketPriority.MEDIUM

            tasks_payload.append(
                {
                    "subject": subject,
                    "description": description,
                    "client_id": int(client_id),
                    "instrument_id": int(instrument_id) if instrument_id else None,
                    "app_id": int(app_id) if app_id else None,
                    "ticket_for": ticket_for,
                    "engineer_id": int(engineer_id) if engineer_id else None,
                    "service_type": service_type,
                    "priority": priority_val,
                    "pm_schedule_id": int(pm_schedule_id_raw) if pm_schedule_id_raw and service_type == "pm-onsite" else None,
                }
            )

        if not tasks_payload:
            flash("Add at least one task with client and a target (instrument or CDS application).", "danger")
            return render_template(
                "weekly_schedules_new.html",
                clients=clients,
                instruments=instruments,
                apps=apps,
                engineers=engineers,
                default_week_start=week_start.strftime("%Y-%m-%d"),
                title=title,
            )

        schedule = WeeklySchedule(
            title=title or f"Week of {week_start.strftime('%Y-%m-%d')}",
            week_start=week_start,
            week_end=week_end,
            created_by_id=current_user.id,
        )
        db.session.add(schedule)
        db.session.flush()

        tasks_created = []
        for payload in tasks_payload:
            task = WeeklyScheduleTask(
                schedule_id=schedule.id,
                client_id=payload["client_id"],
                instrument_id=payload["instrument_id"],
                app_id=payload["app_id"],
                ticket_for=payload["ticket_for"],
                engineer_id=payload["engineer_id"],
                service_type=payload["service_type"],
                subject=payload["subject"],
                description=payload["description"],
                priority=payload["priority"],
                pm_schedule_id=payload["pm_schedule_id"],
            )
            db.session.add(task)
            tasks_created.append((task, payload))

        db.session.flush()

        for task, payload in tasks_created:
            if payload["service_type"] == "pm-onsite" and payload["instrument_id"] and not payload["pm_schedule_id"]:
                auto_doc = f"AUTO-PM-{schedule.id}-{task.id}-{int(datetime.utcnow().timestamp())}"
                pm = PreventiveMaintenanceSchedule(
                    doc_no=auto_doc,
                    description=payload["description"] or f"Weekly schedule task {payload['subject']}",
                    client_id=payload["client_id"],
                    instrument_id=payload["instrument_id"],
                    date=schedule.week_start,
                    task_duration="Weekly schedule coverage",
                    assigned_engineer_id=payload["engineer_id"],
                    ticket_id=None,
                )
                db.session.add(pm)
                db.session.flush()
                task.pm_schedule_id = pm.id
                payload["pm_schedule_id"] = pm.id

            if payload["service_type"] != "pm-onsite":
                ticket = Ticket(
                    ticket_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
                    client_id=task.client_id,
                    assigned_engineer_id=task.engineer_id,
                    assigned_by_id=current_user.id,
                    instrument_id=task.instrument_id if payload["ticket_for"] == "instrument" else None,
                    app_id=task.app_id if payload["ticket_for"] == "app" else None,
                    ticket_for=payload["ticket_for"],
                    reported_by_id=current_user.id,
                    priority=payload["priority"],
                    subject=payload["subject"],
                    description=f"[Weekly Schedule #{schedule.id}] {payload['description']} (Week {schedule.week_start} to {schedule.week_end})",
                    status=TicketStatus.OPEN,
                    started_date=None,
                    is_working=False,
                )
                db.session.add(ticket)
                db.session.flush()
                ticket.ticket_no = generate_ticket_no(ticket.id)
                task.ticket_id = ticket.id

        db.session.commit()
        flash(f"Weekly schedule created with {len(tasks_created)} tasks & tickets.", "success")
        return redirect(url_for("weekly_schedules.detail", schedule_id=schedule.id))

    return render_template(
        "weekly_schedules_new.html",
        clients=clients,
        instruments=instruments,
        apps=apps,
        engineers=engineers,
        default_week_start=default_week_start.strftime("%Y-%m-%d"),
        title="",
    )


@weekly_schedules_bp.route("/<int:schedule_id>")
@login_required
def detail(schedule_id):
    require_engineer_or_admin()
    schedule = WeeklySchedule.query.get_or_404(schedule_id)
    tasks = WeeklyScheduleTask.query.filter_by(schedule_id=schedule.id).order_by(WeeklyScheduleTask.id.asc()).all()
    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    pm_schedules = (
        PreventiveMaintenanceSchedule.query.filter(~PreventiveMaintenanceSchedule.service_logs.any())
        .order_by(PreventiveMaintenanceSchedule.date.asc())
        .all()
    )

    ticket_ids = [t.ticket_id for t in tasks if t.ticket_id]
    logs_by_ticket = {}
    if ticket_ids:
        logs = ServiceLog.query.filter(ServiceLog.ticket_id.in_(ticket_ids)).all()
        for log in logs:
            logs_by_ticket.setdefault(log.ticket_id, []).append(log)

    return render_template(
        "weekly_schedules_detail.html",
        schedule=schedule,
        tasks=tasks,
        logs_by_ticket=logs_by_ticket,
        clients=clients,
        instruments=instruments,
        engineers=engineers,
        pm_schedules=pm_schedules,
    )


@weekly_schedules_bp.route("/<int:schedule_id>/tasks", methods=["POST"])
@login_required
def add_task(schedule_id):
    require_engineer_or_admin()
    schedule = WeeklySchedule.query.get_or_404(schedule_id)

    subject = (request.form.get("subject") or "").strip()
    description = (request.form.get("description") or "").strip()
    client_id = request.form.get("client_id")
    instrument_id = request.form.get("instrument_id")
    engineer_id = request.form.get("engineer_id") or None
    service_type = (request.form.get("service_type") or "pm-onsite").strip()
    pm_schedule_id = request.form.get("pm_schedule_id") or None
    priority_raw = request.form.get("priority") or TicketPriority.MEDIUM.value

    errors = []
    if not subject:
        errors.append("Subject is required.")
    if not client_id or not instrument_id:
        errors.append("Client and instrument are required.")

    try:
        priority_val = TicketPriority(priority_raw)
    except ValueError:
        priority_val = TicketPriority.MEDIUM

    if errors:
        for e in errors:
            flash(e, "danger")
        return redirect(url_for("weekly_schedules.detail", schedule_id=schedule.id))

    pm_id_to_use = int(pm_schedule_id) if pm_schedule_id and service_type == "pm-onsite" else None

    if service_type == "pm-onsite" and not pm_id_to_use:
        auto_doc = f"AUTO-PM-{schedule.id}-{int(datetime.utcnow().timestamp())}"
        pm = PreventiveMaintenanceSchedule(
            doc_no=auto_doc,
            description=description or f"Weekly schedule task {subject}",
            client_id=int(client_id),
            instrument_id=int(instrument_id),
            date=schedule.week_start,
            task_duration="Weekly schedule coverage",
            assigned_engineer_id=int(engineer_id) if engineer_id else None,
            ticket_id=None,
        )
        db.session.add(pm)
        db.session.flush()
        pm_id_to_use = pm.id

    task = WeeklyScheduleTask(
        schedule_id=schedule.id,
        client_id=int(client_id),
        instrument_id=int(instrument_id),
        engineer_id=int(engineer_id) if engineer_id else None,
        service_type=service_type,
        subject=subject,
        description=description,
        priority=priority_val,
        pm_schedule_id=pm_id_to_use,
    )
    db.session.add(task)
    db.session.flush()

    if service_type != "pm-onsite":
        ticket = Ticket(
            ticket_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
            client_id=task.client_id,
            instrument_id=task.instrument_id,
            reported_by_id=current_user.id,
            assigned_engineer_id=int(engineer_id) if engineer_id else None,
            assigned_by_id=current_user.id,
            priority=priority_val,
            subject=subject,
            description=f"[Weekly Schedule #{schedule.id}] {description} (Week {schedule.week_start} to {schedule.week_end})",
            status=TicketStatus.OPEN,
            started_date=None,
            is_working=False,
        )
        db.session.add(ticket)
        db.session.flush()
        ticket.ticket_no = generate_ticket_no(ticket.id)
        task.ticket_id = ticket.id

    db.session.commit()
    flash("Task added and ticket created.", "success")
    return redirect(url_for("weekly_schedules.detail", schedule_id=schedule.id))
