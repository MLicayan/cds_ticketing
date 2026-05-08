from datetime import datetime
import os

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, current_app
from flask_login import login_required, current_user

from werkzeug.utils import secure_filename

from . import db
from .models import (
    PreventiveMaintenanceSchedule,
    Instrument,
    User,
    UserRole,
    PMScheduleComment,
    PMScheduleAttachment,
    Client,
)


pm_schedules_bp = Blueprint("pm_schedules", __name__, template_folder="templates")


@pm_schedules_bp.before_request
def require_staff():
    if not current_user.is_authenticated:
        abort(403)
    if not current_user.has_nav_access("pm_schedules"):
        abort(403)


def generate_doc_no(pm_id: int) -> str:
    return f"PM-{pm_id:06d}"


@pm_schedules_bp.route("/")
@login_required
def index():

    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    
    query = PreventiveMaintenanceSchedule.query
    
    client_id = request.args.get("client_id") or ""
    instrument_id = request.args.get("instrument_id") or ""
    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""
    
    if client_id:
        query = query.filter(PreventiveMaintenanceSchedule.client_id == int(client_id))
        instruments = Instrument.query.filter(Instrument.client_id == int(client_id)).order_by(Instrument.name.asc()).all()
    if instrument_id:
        query = query.filter(PreventiveMaintenanceSchedule.instrument_id == int(instrument_id))
        
    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d")
            query = query.filter(PreventiveMaintenanceSchedule.date >= date_from)
        except ValueError:
            pass
    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d")
            query = query.filter(PreventiveMaintenanceSchedule.date <= date_to)
        except ValueError:
            pass
    current_date = datetime.now().strftime("%Y-%m-%d")
    
    return render_template("pm_schedules_index.html", 
                           schedules=query, 
                           current_date=current_date, 
                           clients=clients, 
                           instruments=instruments,
                           selected_filters={
                                "client_id": client_id,
                                "instrument_id": instrument_id,
                                "date_from": date_from_raw,
                                "date_to": date_to_raw,
                            }
                           )


@pm_schedules_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()

    if request.method == "POST":
        client_id = request.form.get("client_id")
        instrument_id = request.form.get("instrument_id")
        date_raw = request.form.get("date") or ""
        description = request.form.get("description")
        task_duration = request.form.get("task_duration")
        assigned_engineer_id = request.form.get("assigned_engineer_id") or None
        ticket_id = None

        errors = []
        if not client_id:
            errors.append("Client is required.")
        if not instrument_id:
            errors.append("Instrument is required.")
        if not date_raw:
            errors.append("Date is required.")

        instrument = Instrument.query.get(instrument_id) if instrument_id else None
        if instrument and client_id and str(instrument.client_id) != str(client_id):
            errors.append("Selected instrument does not belong to the chosen client.")

        try:
            schedule_date = datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else None
        except ValueError:
            schedule_date = None
            errors.append("Date is invalid.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template(
                "pm_schedules_form.html",
                clients=clients,
                instruments=instruments,
                engineers=engineers,
            )

        schedule = PreventiveMaintenanceSchedule(
            doc_no=f"TEMP-{int(datetime.utcnow().timestamp() * 1000)}",
            client_id=client_id,
            instrument_id=instrument_id,
            date=schedule_date,
            description=description,
            task_duration=task_duration,
            assigned_engineer_id=assigned_engineer_id,
            assigned_by_id=current_user.id,
            ticket_id=ticket_id,
        )
        db.session.add(schedule)
        db.session.flush()
        schedule.doc_no = generate_doc_no(schedule.id)
        db.session.commit()
        flash("Preventive maintenance schedule created.", "success")
        return redirect(url_for("pm_schedules.index"))

    return render_template(
        "pm_schedules_form.html",
        clients=clients,
        instruments=instruments,
        engineers=engineers,
    )


@pm_schedules_bp.route("/<int:schedule_id>/edit", methods=["GET", "POST"])
@login_required
def edit(schedule_id):
    schedule = PreventiveMaintenanceSchedule.query.get_or_404(schedule_id)
    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()

    if request.method == "POST":
        client_id = request.form.get("client_id")
        instrument_id = request.form.get("instrument_id")
        date_raw = request.form.get("date") or ""
        description = request.form.get("description")
        task_duration = request.form.get("task_duration")
        assigned_engineer_id = request.form.get("assigned_engineer_id") or None
        ticket_id = None

        errors = []
        if not client_id:
            errors.append("Client is required.")
        if not instrument_id:
            errors.append("Instrument is required.")
        if not date_raw:
            errors.append("Date is required.")

        instrument = Instrument.query.get(instrument_id) if instrument_id else None
        if instrument and client_id and str(instrument.client_id) != str(client_id):
            errors.append("Selected instrument does not belong to the chosen client.")

        try:
            schedule_date = datetime.strptime(date_raw, "%Y-%m-%d").date() if date_raw else None
        except ValueError:
            schedule_date = None
            errors.append("Date is invalid.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template(
                "pm_schedules_form.html",
                schedule=schedule,
                clients=clients,
                instruments=instruments,
                engineers=engineers,
            )

        schedule.client_id = client_id
        schedule.instrument_id = instrument_id
        schedule.date = schedule_date
        schedule.description = description
        schedule.task_duration = task_duration
        schedule.assigned_engineer_id = assigned_engineer_id
        schedule.ticket_id = ticket_id
        if not schedule.assigned_by_id:
            schedule.assigned_by_id = current_user.id
        db.session.commit()
        flash("Preventive maintenance schedule updated.", "success")
        return redirect(url_for("pm_schedules.index"))

    return render_template(
        "pm_schedules_form.html",
        schedule=schedule,
        clients=clients,
        instruments=instruments,
        engineers=engineers,
    )


@pm_schedules_bp.route("/<int:schedule_id>", methods=["GET", "POST"])
@login_required
def detail(schedule_id):
    schedule = PreventiveMaintenanceSchedule.query.get_or_404(schedule_id)
    if request.method == "POST":
        comment_text = request.form.get("comment_text")
        file = request.files.get("attachment")
        did_something = False

        if comment_text:
            comment = PMScheduleComment(
                schedule_id=schedule.id,
                user_id=current_user.id,
                comment_text=comment_text,
            )
            db.session.add(comment)
            did_something = True

        if file and file.filename:
            upload_folder = current_app.config.get("UPLOAD_FOLDER_PM_SCHEDULES")
            os.makedirs(upload_folder, exist_ok=True)
            safe_name = secure_filename(file.filename)
            stored_name = f"{schedule.id}_{int(datetime.utcnow().timestamp())}_{safe_name}"
            filepath = os.path.join(upload_folder, stored_name)
            file.save(filepath)
            att = PMScheduleAttachment(
                schedule_id=schedule.id,
                user_id=current_user.id,
                stored_filename=stored_name,
                original_filename=file.filename,
                content_type=file.mimetype,
                file_size=os.path.getsize(filepath),
            )
            db.session.add(att)
            did_something = True

        if did_something:
            db.session.commit()
            flash("Update saved.", "success")
        else:
            flash("Nothing to save.", "warning")
        return redirect(url_for("pm_schedules.detail", schedule_id=schedule.id))

    comments = schedule.comments or []
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    service_logs = schedule.service_logs if hasattr(schedule, "service_logs") else []
    return render_template(
        "pm_schedules_detail.html",
        schedule=schedule,
        comments=comments,
        engineers=engineers,
        service_logs=service_logs,
    )

@pm_schedules_bp.route("/<int:schedule_id>/assign", methods=["POST"])
@login_required
def update_assignee(schedule_id):
    schedule = PreventiveMaintenanceSchedule.query.get_or_404(schedule_id)
    if current_user.role == UserRole.CLIENT:
        abort(403)

    engineer_id_raw = request.form.get("engineer_id")
    if not engineer_id_raw:
        schedule.assigned_engineer_id = None
        schedule.assigned_by_id = current_user.id
        db.session.commit()
        flash("Assigned engineer cleared.", "success")
        return redirect(url_for("pm_schedules.detail", schedule_id=schedule.id))

    engineer = User.query.filter(User.id == engineer_id_raw, User.role == UserRole.ENGINEER).first()
    if not engineer:
        flash("Invalid engineer selection.", "danger")
        return redirect(url_for("pm_schedules.detail", schedule_id=schedule.id))

    schedule.assigned_engineer_id = engineer.id
    schedule.assigned_by_id = current_user.id
    db.session.commit()
    flash("Engineer assigned.", "success")
    return redirect(url_for("pm_schedules.detail", schedule_id=schedule.id))
