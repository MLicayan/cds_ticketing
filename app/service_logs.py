from werkzeug.utils import secure_filename
import os
import csv
import io
import base64
from datetime import datetime, date, timedelta
import calendar
from decimal import Decimal, InvalidOperation
import pprint

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response, current_app, abort
from flask_login import login_required, current_user
from . import APP_TIMEZONE, db
from .models import (
    ServiceLog,
    ServiceLogPart,
    Client,
    Instrument,
    User,
    UserRole,
    Ticket,
    ServiceLogAttachment,
    TicketStatus,
    Part,
    App,
    PreventiveMaintenanceSchedule,
    TicketPriority,
)


service_logs_bp = Blueprint("service_logs", __name__, template_folder="templates")
READ_ONLY_ROLES = (UserRole.CLIENT, UserRole.SALES)


@service_logs_bp.before_request
def require_service_logs_nav_access():
    if current_user.is_authenticated and not current_user.has_nav_access("service_logs"):
        abort(403)

@service_logs_bp.route("/")
@login_required
def index():
    is_client = current_user.role == UserRole.CLIENT
    is_engineer = current_user.role == UserRole.ENGINEER

    if is_client and not current_user.client_id:
        abort(403)

    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()

    query = ServiceLog.query

    client_id = request.args.get("client_id") or ""
    instrument_id = request.args.get("instrument_id") or ""
    engineer_id = request.args.get("engineer_id") or ""
    service_type = request.args.get("service_type") or ""
    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""

    if is_client:
        client_id = str(current_user.client_id)
        query = query.filter(ServiceLog.client_id == current_user.client_id)
        instruments = Instrument.query.filter(Instrument.client_id == current_user.client_id).order_by(Instrument.name.asc()).all()
        clients = [c for c in clients if c.id == current_user.client_id]
        engineers = User.query.filter(
            User.role == UserRole.ENGINEER,
            User.client_id == current_user.client_id,
        ).order_by(User.full_name.asc()).all()
    elif is_engineer:
        engineer_id = ""
        query = query.filter(ServiceLog.engineer_id == current_user.id)
        engineers = [current_user]
    if client_id:
        query = query.filter(ServiceLog.client_id == int(client_id))
    if instrument_id:
        query = query.filter(ServiceLog.instrument_id == int(instrument_id))
    if engineer_id:
        query = query.filter(ServiceLog.engineer_id == int(engineer_id))
    if service_type:
        query = query.filter(ServiceLog.service_type == service_type)

    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date()
            query = query.filter(ServiceLog.visit_date >= date_from)
        except ValueError:
            pass
    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date()
            query = query.filter(ServiceLog.visit_date <= date_to)
        except ValueError:
            pass

    logs = query.order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc()).all()
    current_date = datetime.now().strftime("%Y-%m-%d")

    return render_template(
        "service_logs/index.html",
        logs=logs,
        current_date=current_date,
        clients=clients,
        instruments=instruments,
        engineers=engineers,
        selected_filters={
            "client_id": client_id,
            "instrument_id": instrument_id,
            "engineer_id": engineer_id,
            "service_type": service_type,
            "date_from": date_from_raw,
            "date_to": date_to_raw,
        },
    )


@service_logs_bp.route("/gantt")
@login_required
def gantt():
    group_by = request.args.get("group_by", "client")
    view_mode = request.args.get("view_mode", "month")
    start_raw = request.args.get("start")
    range_start_raw = request.args.get("date_from")
    range_end_raw = request.args.get("date_to")
    today_date = datetime.utcnow().date()

    def month_start(d: date) -> date:
        return date(d.year, d.month, 1)

    def add_months(d: date, months: int) -> date:
        month = d.month - 1 + months
        year = d.year + month // 12
        month = month % 12 + 1
        day = min(d.day, calendar.monthrange(year, month)[1])
        return date(year, month, day)

    def parse_date(raw, default):
        if not raw:
            return default
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            return default

    base_start = parse_date(start_raw, today_date)

    if view_mode == "month":
        window_start = month_start(base_start)
        window_end = add_months(window_start, 1) - timedelta(days=1)
        nav_prev = add_months(window_start, -1)
        nav_next = add_months(window_start, 1)
    elif view_mode == "biweekly":
        window_start = base_start
        window_end = window_start + timedelta(days=13)
        nav_prev = window_start - timedelta(days=14)
        nav_next = window_start + timedelta(days=14)
    elif view_mode == "week":
        window_start = base_start
        window_end = window_start + timedelta(days=6)
        nav_prev = window_start - timedelta(days=7)
        nav_next = window_start + timedelta(days=7)
    elif view_mode == "range":
        range_start = parse_date(range_start_raw, base_start)
        range_end = parse_date(range_end_raw, range_start)
        if range_end < range_start:
            range_start, range_end = range_end, range_start
        window_start = range_start
        window_end = range_end
        nav_prev = window_start
        nav_next = window_start
    else:
        window_start = month_start(base_start)
        window_end = add_months(window_start, 1) - timedelta(days=1)
        nav_prev = add_months(window_start, -1)
        nav_next = add_months(window_start, 1)

    window_total_days = max((window_end - window_start).days + 1, 1)

    query = ServiceLog.query
    if current_user.role == UserRole.CLIENT:
        query = query.filter(ServiceLog.client_id == current_user.client_id)
    elif current_user.role == UserRole.ENGINEER:
        query = query.filter(ServiceLog.engineer_id == current_user.id)

    logs = query.order_by(ServiceLog.visit_date.asc()).all()

    def grouping_label(log: ServiceLog) -> str:
        if group_by == "instrument":
            return log.instrument.display_label() if log.instrument else "No Instrument"
        if group_by == "assignee":
            return log.engineer.full_name or log.engineer.username if log.engineer else "Unassigned"
        return log.client.name if log.client else "No Client"

    def status_style(status_after: str):
        if not status_after:
            return ("#f1f5f9", "#6c757d")
        normalized = (status_after or "").lower()
        if "operational" in normalized or normalized == "operational":
            return ("#e2f7e2", "#0f5132")
        if "non" in normalized or "down" in normalized:
            return ("#fde2e2", "#842029")
        return ("#e7eaf6", "#343a40")

    tasks = []
    for log in logs:
        start_date = log.visit_date or (log.created_at.date() if log.created_at else window_start)
        end_date = start_date

        display_start = max(start_date, window_start)
        display_end = min(end_date, window_end)
        if display_end < window_start or display_start > window_end:
            continue

        bg_color, text_color = status_style(log.status_after)
        tasks.append(
            {
                "log": log,
                "start": start_date,
                "end": end_date,
                "display_start": display_start,
                "display_end": display_end,
                "status_after": log.status_after or "N/A",
                "group_label": grouping_label(log),
                "bg_color": bg_color,
                "text_color": text_color,
            }
        )

    day_headers = []
    current = window_start
    while current <= window_end:
        day_headers.append(
            {
                "date": current,
                "day": current.day,
                "dow": current.strftime("%a"),
                "month_label": current.strftime("%B %Y"),
            }
        )
        current += timedelta(days=1)

    month_spans = []
    if day_headers:
        current_label = day_headers[0]["month_label"]
        count = 0
        for d in day_headers:
            if d["month_label"] != current_label:
                month_spans.append({"label": current_label, "days": count})
                current_label = d["month_label"]
                count = 0
            count += 1
        month_spans.append({"label": current_label, "days": count})

    for task in tasks:
        start_offset = (task["display_start"] - window_start).days
        duration_days = max((task["display_end"] - task["display_start"]).days + 1, 1)
        task["left_pct"] = (start_offset / window_total_days) * 100
        task["width_pct"] = (duration_days / window_total_days) * 100

    groups_map = {}
    for task in tasks:
        label = task["group_label"]
        groups_map.setdefault(label, []).append(task)
    grouped_tasks = [{"label": k, "items_list": v} for k, v in sorted(groups_map.items(), key=lambda x: x[0].lower())]

    return render_template(
        "service_logs/gantt.html",
        grouped_tasks=grouped_tasks,
        window_start=window_start,
        window_end=window_end,
        day_headers=day_headers,
        month_spans=month_spans,
        group_by=group_by,
        view_mode=view_mode,
        prev_start=nav_prev,
        next_start=nav_next,
        today_start=month_start(today_date),
        range_start_raw=range_start_raw,
        range_end_raw=range_end_raw,
    )

@service_logs_bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if current_user.role in READ_ONLY_ROLES:
        abort(403)

    clients = Client.query.order_by(Client.name.asc()).all()
    instruments = Instrument.query.order_by(Instrument.name.asc()).all()
    apps = App.query.order_by(App.name.asc()).all()
    engineers = User.query.filter(User.role == UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    if current_user.role == UserRole.ENGINEER:
        engineers = [current_user]
    parts = Part.query.order_by(Part.name.asc()).all()
    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    ticket_id = request.args.get("ticket_id") if request.method == "GET" else request.form.get("ticket_id")
    pm_schedule_id = request.args.get("pm_schedule_id") if request.method == "GET" else request.form.get("pm_schedule_id")
    ticket = Ticket.query.get(ticket_id) if ticket_id else None
    pm_schedule = PreventiveMaintenanceSchedule.query.get(pm_schedule_id) if pm_schedule_id else None

    if request.method == "POST":
        if pm_schedule:
            client_id = pm_schedule.client_id
            instrument_id = pm_schedule.instrument_id
        else:
            client_id = request.form.get("client_id")
            instrument_id = request.form.get("instrument_id")
            
        engineer_id = current_user.id if current_user.role == UserRole.ENGINEER else (request.form.get("engineer_id") or current_user.id)
        
        if pm_schedule:
            service_type = "pm-onsite"
        else:
            service_type_raw = request.form.get("service_type")
            service_type = (service_type_raw or "corrective-remote").strip().lower()
            
        visit_date_raw = request.form.get("visit_date") or ""
        problem_description = request.form.get("problem_description")
        root_cause = request.form.get("root_cause")
        action_taken = request.form.get("action_taken")
        recommendations = request.form.get("recommendations")
        status_after = request.form.get("status_after")
        start_time_raw = request.form.get("start_time") or ""
        end_time_raw = request.form.get("end_time") or ""
        is_monitor_raw = request.form.get("is_monitor")
        is_monitor = is_monitor_raw not in (None, "", "0", "false")

        monitored_days_raw = request.form.get("monitored_days") or "0"
        try:
            monitored_days = int(monitored_days_raw)
        except ValueError:
            monitored_days = 0

        if not is_monitor:
            monitored_days = 0
        
        confirmed_by = request.form.get("confirmed_by")
        confirmed_by_position = request.form.get("confirmed_by_position")
        photo = request.files.get("photo")
        part_ids = request.form.getlist("part_ids[]") or request.form.getlist("part_ids")
        part_nos = request.form.getlist("part_nos[]")
        part_qtys = request.form.getlist("part_qtys[]")
        part_prices = request.form.getlist("part_prices[]")
        part_totals = request.form.getlist("part_totals[]")
        part_warranties = request.form.getlist("part_warranties[]")
        signature_data = request.form.get("signature_data")
        signature_bytes = None

        remote_service_types = ["corrective-remote", "calibration-remote"]
        signature_required = service_type not in remote_service_types

        errors = []
        if not (client_id and instrument_id and engineer_id):
            errors.append("Client, instrument and engineer are required.")
        if not confirmed_by:
            errors.append("Confirm By is required.")
        if not confirmed_by_position:
            errors.append("Position is required.")
        if not photo or not photo.filename:
            errors.append("A confirmation photo is required.")

        if is_monitor and monitored_days <= 0:
            errors.append("Please enter monitored days when monitoring is enabled.")
            
        if signature_required:
            if not signature_data:
                errors.append("A customer signature is required.")
                
        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template(
                "service_logs/new.html",
                clients=clients,
                instruments=instruments,
                engineers=engineers,
                parts=parts,
                ticket=ticket,
                pm_schedule=pm_schedule,
                default_visit_date=visit_date_raw or today_str,
            )
            
        try:
            sig_stripped = signature_data.split(",", 1)[1] if "," in signature_data else signature_data
            signature_bytes = base64.b64decode(sig_stripped)
        except Exception:
            errors.append("Invalid signature data. Please recapture.")


        try:
            visit_date = datetime.strptime(visit_date_raw, "%Y-%m-%d").date() if visit_date_raw else datetime.utcnow().date()
        except ValueError:
            visit_date = datetime.utcnow().date()

        def parse_time(val):
            try:
                return datetime.strptime(val, "%H:%M").time()
            except ValueError:
                return None

        start_time = parse_time(start_time_raw)
        end_time = parse_time(end_time_raw)

        log = ServiceLog(
            ticket_id=ticket.id if ticket else None,
            pm_schedule_id=pm_schedule.id if pm_schedule else None,
            client_id=client_id,
            instrument_id=instrument_id,
            engineer_id=engineer_id,
            service_type=service_type,
            visit_date=visit_date,
            start_time=start_time,
            end_time=end_time,
            problem_description=problem_description,
            root_cause=root_cause,
            action_taken=action_taken,
            recommendations=recommendations,
            confirmed_by=confirmed_by,
            confirmed_by_position=confirmed_by_position,
            status_after=status_after,
            is_monitor=is_monitor,
            monitored_days=monitored_days,
        )

        db.session.add(log)
        db.session.flush()

        parts_rows = []
        clean_part_ids = []
        max_rows = max(len(part_nos), len(part_qtys), len(part_prices), len(part_totals), len(part_ids), len(part_warranties))
        for idx in range(max_rows):
            pid = part_ids[idx] if idx < len(part_ids) else ""
            part_no = (part_nos[idx] if idx < len(part_nos) else "").strip()
            qty_raw = (part_qtys[idx] if idx < len(part_qtys) else "").strip()
            price_raw = (part_prices[idx] if idx < len(part_prices) else "").strip()
            total_raw = (part_totals[idx] if idx < len(part_totals) else "").strip()
            warranty_raw = part_warranties[idx] if idx < len(part_warranties) else "0"
            warranty = warranty_raw == "1"

            if not any([pid, part_no, qty_raw, price_raw, total_raw]):
                continue

            try:
                qty_val = Decimal(qty_raw) if qty_raw else Decimal("0")
            except (InvalidOperation, ValueError):
                qty_val = Decimal("0")
            try:
                price_val = Decimal(price_raw) if price_raw else Decimal("0")
            except (InvalidOperation, ValueError):
                price_val = Decimal("0")
            try:
                total_val = Decimal(total_raw) if total_raw else (qty_val * price_val)
            except (InvalidOperation, ValueError):
                total_val = qty_val * price_val

            parts_rows.append(
                {
                    "part_id": pid,
                    "part_no": part_no or "",
                    "qty": qty_val,
                    "price": price_val,
                    "total": total_val,
                    "warranty": warranty,
                }
            )
            if pid:
                clean_part_ids.append(pid)

        if parts_rows:
            part_lookup = {}
            if clean_part_ids:
                part_lookup = {str(p.id): p.name for p in Part.query.filter(Part.id.in_(clean_part_ids)).all()}

            for row in parts_rows:
                slp = ServiceLogPart(
                    service_log_id=log.id,
                    part_id=int(row["part_id"]) if row["part_id"] else None,
                    part_no=row["part_no"],
                    qty=row["qty"],
                    price=row["price"],
                    total=row["total"],
                    under_warranty=row["warranty"],
                )
                db.session.add(slp)

            summary_lines = []
            for row in parts_rows:
                part_label = part_lookup.get(str(row["part_id"]), row["part_id"] or "N/A")
                summary_lines.append(
                    f"Part: {part_label} | Part No: {row['part_no'] or '-'} | Qty: {row['qty']} | Price: {row['price']} | Total: {row['total']} | Warranty: {'Yes' if row['warranty'] else 'No'}"
                )
            log.parts_used = "\n".join(summary_lines)

        upload_folder = current_app.config.get("UPLOAD_FOLDER_SERVICE_LOGS")
        os.makedirs(upload_folder, exist_ok=True)
        safe_name = secure_filename(photo.filename)
        stored_name = f"{log.id}_{int(datetime.utcnow().timestamp())}_{safe_name}"
        filepath = os.path.join(upload_folder, stored_name)
        photo.save(filepath)

        log.confirm_photo_name = stored_name

        att = ServiceLogAttachment(
            service_log_id=log.id,
            user_id=current_user.id,
            stored_filename=stored_name,
            original_filename=photo.filename,
            content_type=photo.mimetype,
            file_size=os.path.getsize(filepath),
        )
        db.session.add(att)

        if signature_bytes:
            sig_name = f"{log.id}_{int(datetime.utcnow().timestamp())}_signature.png"
            sig_path = os.path.join(upload_folder, sig_name)
            with open(sig_path, "wb") as f:
                f.write(signature_bytes)
            sig_attachment = ServiceLogAttachment(
                service_log_id=log.id,
                user_id=current_user.id,
                stored_filename=sig_name,
                original_filename="signature.png",
                content_type="image/png",
                file_size=os.path.getsize(sig_path),
            )
            db.session.add(sig_attachment)

        if ticket and ticket.status != TicketStatus.CLOSED:
            if status_after == "operational":
                ticket.status = TicketStatus.RESOLVED
                ticket.closed_at = datetime.now(APP_TIMEZONE).replace(tzinfo=None)
            else:
                ticket.status = TicketStatus.IN_PROGRESS

        db.session.commit()
        flash("Service log saved.", "success")

        if ticket:
            return redirect(url_for("tickets.detail", ticket_id=ticket.id))

        return redirect(url_for("service_logs.index"))

    default_service_for = "instrument" if current_user.role == UserRole.ENGINEER else "app"
    return render_template(
        "service_logs/new.html",
        clients=clients,
        instruments=instruments,
        engineers=engineers,
        parts=parts,
        ticket=ticket,
        pm_schedule=pm_schedule,
        default_visit_date=today_str,
        apps=apps,
        default_service_for=default_service_for,
    )


@service_logs_bp.route("/<int:log_id>", methods=["GET", "POST"])
@login_required
def detail(log_id):
    log = ServiceLog.query.get_or_404(log_id)

    if current_user.role == UserRole.CLIENT and log.client_id != current_user.client_id:
        abort(403)
    if current_user.role == UserRole.ENGINEER and log.engineer_id != current_user.id:
        abort(403)

    if current_user.role in READ_ONLY_ROLES and request.method == "POST":
        abort(403)

    if request.method == "POST":
        if request.form.get("update_log"):
            if current_user.role != UserRole.ADMIN and (not log.engineer or log.engineer.id != current_user.id):
                abort(403)

            visit_date_raw = request.form.get("visit_date") or ""
            service_type = (request.form.get("service_type") or log.service_type or "").strip()
            status_after = request.form.get("status_after") or log.status_after
            problem_description = request.form.get("problem_description")
            root_cause = request.form.get("root_cause")
            action_taken = request.form.get("action_taken")
            recommendations = request.form.get("recommendations")
            confirmed_by = request.form.get("confirmed_by") or log.confirmed_by
            confirmed_by_position = request.form.get("confirmed_by_position") or log.confirmed_by_position
            start_time_raw = request.form.get("start_time") or ""
            end_time_raw = request.form.get("end_time") or log.end_time

            is_monitor_raw = request.form.get("is_monitor")
            is_monitor = is_monitor_raw not in (None, "", "0", "false")
            monitored_days_raw = request.form.get("monitored_days") or "0"
            try:
                monitored_days = int(monitored_days_raw)
            except ValueError:
                monitored_days = 0
            if not is_monitor:
                monitored_days = 0

            try:
                visit_date = datetime.strptime(visit_date_raw, "%Y-%m-%d").date() if visit_date_raw else log.visit_date
            except ValueError:
                visit_date = log.visit_date
            def parse_time(val, fallback=None):
                try:
                    return datetime.strptime(val, "%H:%M").time()
                except ValueError:
                    return fallback

            start_time = parse_time(start_time_raw, log.start_time)
            end_time = parse_time(end_time_raw, log.end_time)

            log.visit_date = visit_date
            log.service_type = service_type
            log.status_after = status_after
            log.problem_description = problem_description
            log.root_cause = root_cause
            log.action_taken = action_taken
            log.recommendations = recommendations
            log.confirmed_by = confirmed_by
            log.confirmed_by_position = confirmed_by_position
            log.start_time = start_time
            log.end_time = end_time
            log.is_monitor = is_monitor
            log.monitored_days = monitored_days

            db.session.commit()
            flash("Service log updated.", "success")
            return redirect(url_for("service_logs.detail", log_id=log.id))
        else:
            file = request.files.get("attachment")
            if file and file.filename:
                upload_folder = current_app.config.get("UPLOAD_FOLDER_SERVICE_LOGS")
                os.makedirs(upload_folder, exist_ok=True)
                safe_name = secure_filename(file.filename)
                stored_name = f"{log.id}_{int(datetime.utcnow().timestamp())}_{safe_name}"
                filepath = os.path.join(upload_folder, stored_name)
                file.save(filepath)
                att = ServiceLogAttachment(
                    service_log_id=log.id,
                    user_id=current_user.id,
                    stored_filename=stored_name,
                    original_filename=file.filename,
                    content_type=file.mimetype,
                    file_size=os.path.getsize(filepath),
                )
                db.session.add(att)
                db.session.commit()
                flash("Attachment uploaded.", "success")
            else:
                flash("No file selected.", "warning")
            return redirect(url_for("service_logs.detail", log_id=log.id))
    return render_template("service_logs/detail.html", log=log)


@service_logs_bp.route("/<int:log_id>/pdf")
@login_required
def pdf(log_id):
    log = ServiceLog.query.get_or_404(log_id)
    if current_user.role == UserRole.CLIENT and log.client_id != current_user.client_id:
        abort(403)
    if current_user.role == UserRole.ENGINEER and log.engineer_id != current_user.id:
        abort(403)
    work_duration = None
    if log.start_time and log.end_time:
        base_date = log.visit_date or datetime.utcnow().date()
        start_dt = datetime.combine(base_date, log.start_time)
        end_dt = datetime.combine(base_date, log.end_time)
        if end_dt < start_dt:
            end_dt += timedelta(days=1)
        diff = end_dt - start_dt
        hours = diff.seconds // 3600
        minutes = (diff.seconds % 3600) // 60
        work_duration = f"{hours}h {minutes}m"

    return render_template("service_logs/pdf.html", log=log, work_duration=work_duration)


@service_logs_bp.route("/<int:log_id>/signature", methods=["POST"])
@login_required
def update_signature(log_id):
    # return "", 204
    log = ServiceLog.query.get_or_404(log_id)
    if current_user.role in READ_ONLY_ROLES:
        abort(403)
    if current_user.role == UserRole.ENGINEER and log.engineer_id != current_user.id:
        abort(403)

    
    if not log.signature_attachment:
        signature_data = request.form.get("signature_data") or ""
        if not signature_data:
            flash("Signature data is required.", "danger")
            return redirect(url_for("service_logs.detail", log_id=log.id))

        try:
            header, encoded = signature_data.split(",", 1) if "," in signature_data else ("", signature_data)
            sig_bytes = base64.b64decode(encoded)
        except Exception:
            flash("Invalid signature data. Please recapture the signature.", "danger")
            return redirect(url_for("service_logs.detail", log_id=log.id))

        upload_folder = current_app.config.get("UPLOAD_FOLDER_SERVICE_LOGS")
        if not upload_folder:
            flash("Signature upload folder is not configured.", "danger")
            return redirect(url_for("service_logs.detail", log_id=log.id))

        os.makedirs(upload_folder, exist_ok=True)
        filename = f"{log.id}_signature_{int(datetime.utcnow().timestamp())}.png"
        filepath = os.path.join(upload_folder, filename)
        with open(filepath, "wb") as f:
            f.write(sig_bytes)
        attachment = ServiceLogAttachment(
            service_log_id=log.id,
            user_id=current_user.id,
            stored_filename=filename,
            original_filename="signature.png",
            content_type="image/png",
            file_size=len(sig_bytes),
        )
        
        db.session.add(attachment)
    db.session.commit()
    flash("Signature updated.", "success")
    return redirect(url_for("service_logs.detail", log_id=log.id))


@service_logs_bp.route("/export")
@login_required
def export():
    query = ServiceLog.query

    if current_user.role == UserRole.CLIENT:
        if not current_user.client_id:
            abort(403)
        query = query.filter(ServiceLog.client_id == current_user.client_id)
    elif current_user.role == UserRole.ENGINEER:
        query = query.filter(ServiceLog.engineer_id == current_user.id)

    client_id = request.args.get("client_id") or ""
    instrument_id = request.args.get("instrument_id") or ""
    engineer_id = request.args.get("engineer_id") or ""
    service_type = request.args.get("service_type") or ""
    date_from_raw = request.args.get("date_from") or ""
    date_to_raw = request.args.get("date_to") or ""

    if client_id:
        query = query.filter(ServiceLog.client_id == int(client_id))
    if instrument_id:
        query = query.filter(ServiceLog.instrument_id == int(instrument_id))
    if current_user.role == UserRole.ENGINEER:
        engineer_id = ""
    if engineer_id:
        query = query.filter(ServiceLog.engineer_id == int(engineer_id))
    if service_type:
        query = query.filter(ServiceLog.service_type == service_type)

    if date_from_raw:
        try:
            date_from = datetime.strptime(date_from_raw, "%Y-%m-%d").date()
            query = query.filter(ServiceLog.visit_date >= date_from)
        except ValueError:
            pass
    if date_to_raw:
        try:
            date_to = datetime.strptime(date_to_raw, "%Y-%m-%d").date()
            query = query.filter(ServiceLog.visit_date <= date_to)
        except ValueError:
            pass

    logs = query.order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc()).all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID",
        "Client",
        "Instrument",
        "Engineer",
        "Service Type",
        "Visit Date",
        "Problem Description",
        "Action Taken",
        "Confirmed By",
        "Position",
        "Created At",
    ])

    for log in logs:
        writer.writerow([
            log.id,
            log.client.name if log.client else "",
            log.instrument.display_label() if log.instrument else "",
            log.engineer.full_name or log.engineer.username if log.engineer else "",
            log.service_type,
            log.visit_date.strftime("%Y-%m-%d") if log.visit_date else "",
            (log.problem_description or "").replace("\n", " "),
            (log.action_taken or "").replace("\n", " "),
            log.confirmed_by or "",
            log.confirmed_by_position or "",
            log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else "",
        ])

    resp = Response(output.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = "attachment; filename=service_logs_export.csv"
    return resp
