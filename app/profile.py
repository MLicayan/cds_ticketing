from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user

from . import db
from .models import App, Client, Instrument, User, UserRole


profile_bp = Blueprint("profile", __name__, template_folder="templates")


def client_admin_required():
    if not current_user.is_authenticated or current_user.role != UserRole.CLIENT_ADMIN:
        abort(403)
    if not current_user.client_id:
        abort(403)


def _current_client_or_404():
    client = Client.query.get_or_404(current_user.client_id)
    return client

@profile_bp.route("/profile", methods=["GET", "POST"])
@login_required
def index():
    client = _current_client_or_404() if current_user.role == UserRole.CLIENT_ADMIN and current_user.client_id else None
    apps = App.query.order_by(App.name.asc()).all() if client else []
    instruments = Instrument.query.filter(Instrument.client_id == client.id).order_by(Instrument.name.asc()).all() if client else []

    if request.method == "POST":
        client_admin_required()
        client.name = request.form.get("name")
        client.client_code = request.form.get("client_code")
        client.address = request.form.get("address")
        client.contact_person = request.form.get("contact_person")
        client.contact_number = request.form.get("contact_number")
        client.email = request.form.get("email")
        app_ids = request.form.getlist("app_ids")
        client.apps = App.query.filter(App.id.in_(app_ids)).all() if app_ids else []

        if not client.name:
            flash("Hospital name is required.", "danger")
            return render_template(
                "profile/index.html",
                client=client,
                client_users=[u for u in client.users if u.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)],
                apps=apps,
                instruments=instruments,
            )

        db.session.commit()
        flash("Profile updated.", "success")
        return redirect(url_for("profile.index"))

    return render_template(
        "profile/index.html",
        client=client,
        client_users=[u for u in client.users if u.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)] if client else [],
        apps=apps,
        instruments=instruments,
    )


@profile_bp.route("/profile/account", methods=["POST"])
@login_required
def update_account():
    full_name = (request.form.get("full_name") or "").strip()
    contact_number = (request.form.get("contact_number") or "").strip()
    if not full_name:
        flash("Full name is required.", "danger")
        return redirect(url_for("profile.index"))
    if not contact_number:
        flash("Contact number is required.", "danger")
        return redirect(url_for("profile.index"))

    current_user.full_name = full_name
    current_user.contact_number = contact_number
    db.session.commit()
    flash("Account profile updated.", "success")
    return redirect(url_for("profile.index"))


@profile_bp.route("/profile/password", methods=["POST"])
@login_required
def change_password():
    new_password = request.form.get("new_password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if len(new_password) < 6:
        flash("New password must be at least 6 characters.", "danger")
        return redirect(url_for("profile.index"))
    if new_password != confirm_password:
        flash("New password and confirmation do not match.", "danger")
        return redirect(url_for("profile.index"))

    current_user.set_password(new_password)
    db.session.commit()
    flash("Password changed successfully.", "success")
    return redirect(url_for("profile.index"))


@profile_bp.route("/profile/users", methods=["POST"])
@login_required
def add_user():
    client_admin_required()
    client = _current_client_or_404()
    username = (request.form.get("username") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    contact_number = (request.form.get("contact_number") or "").strip()
    password = (request.form.get("password") or "").strip()
    errors = []
    if not username:
        errors.append("Username is required.")
    if not password:
        errors.append("Password is required.")
    if username and User.query.filter_by(username=username).first():
        errors.append("Username already exists.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        return redirect(url_for("profile.index"))

    user = User(
        username=username,
        full_name=full_name,
        contact_number=contact_number,
        role=UserRole.CLIENT,
        client_id=client.id,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash("User added.", "success")
    return redirect(url_for("profile.index"))


@profile_bp.route("/profile/users/<int:user_id>/edit", methods=["POST"])
@login_required
def update_user(user_id):
    client_admin_required()
    client = _current_client_or_404()
    user = User.query.get_or_404(user_id)
    if user.client_id != client.id or user.role not in (UserRole.CLIENT, UserRole.CLIENT_ADMIN):
        abort(404)

    username = (request.form.get("username") or "").strip()
    full_name = (request.form.get("full_name") or "").strip()
    contact_number = (request.form.get("contact_number") or "").strip()
    new_password = (request.form.get("password") or "").strip()

    errors = []
    if not username:
        errors.append("Username is required.")
    existing = User.query.filter(User.username == username, User.id != user.id).first()
    if existing:
        errors.append("Username already exists.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        return redirect(url_for("profile.index"))

    user.username = username
    user.full_name = full_name
    user.contact_number = contact_number
    if new_password:
        user.set_password(new_password)
    db.session.commit()
    flash("User updated.", "success")
    return redirect(url_for("profile.index"))


@profile_bp.route("/profile/users/<int:user_id>/delete", methods=["POST"])
@login_required
def delete_user(user_id):
    client_admin_required()
    client = _current_client_or_404()
    user = User.query.get_or_404(user_id)
    if user.client_id != client.id or user.role not in (UserRole.CLIENT, UserRole.CLIENT_ADMIN):
        abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("profile.index"))

    db.session.delete(user)
    db.session.commit()
    flash("User deleted.", "info")
    return redirect(url_for("profile.index"))
