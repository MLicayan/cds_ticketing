import os
from flask import Blueprint, current_app, send_from_directory, abort
from flask_login import login_required, current_user

from .models import TicketAttachment, ServiceLogAttachment, PMScheduleAttachment, UserRole

uploads_bp = Blueprint("uploads", __name__)
CLIENT_SCOPED_ROLES = (UserRole.CLIENT, UserRole.CLIENT_ADMIN)


@uploads_bp.route("/tickets/<path:filename>")
@login_required
def ticket_file(filename):
    upload_folder = current_app.config.get("UPLOAD_FOLDER_TICKETS")
    if not upload_folder:
        abort(404)
    attachment = TicketAttachment.query.filter_by(stored_filename=filename).first()
    if not attachment:
        abort(404)
    if current_user.role in CLIENT_SCOPED_ROLES:
        if not attachment.ticket or attachment.ticket.client_id != current_user.client_id:
            abort(403)
    return send_from_directory(upload_folder, filename, as_attachment=False)


@uploads_bp.route("/service-logs/<path:filename>")
@login_required
def service_log_file(filename):
    upload_folder = current_app.config.get("UPLOAD_FOLDER_SERVICE_LOGS")
    if not upload_folder:
        abort(404)
    attachment = ServiceLogAttachment.query.filter_by(stored_filename=filename).first()
    if not attachment:
        abort(404)
    if current_user.role in CLIENT_SCOPED_ROLES:
        if not attachment.service_log or attachment.service_log.client_id != current_user.client_id:
            abort(403)
    return send_from_directory(upload_folder, filename, as_attachment=False)


@uploads_bp.route("/pm-schedules/<path:filename>")
@login_required
def pm_schedule_file(filename):
    upload_folder = current_app.config.get("UPLOAD_FOLDER_PM_SCHEDULES")
    if not upload_folder:
        abort(404)
    attachment = PMScheduleAttachment.query.filter_by(stored_filename=filename).first()
    if not attachment:
        abort(404)
    return send_from_directory(upload_folder, filename, as_attachment=False)
