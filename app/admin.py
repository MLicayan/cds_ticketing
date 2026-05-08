from flask import Blueprint, render_template, request, redirect, url_for, flash, abort
from flask_login import login_required, current_user
from datetime import datetime
from decimal import Decimal, InvalidOperation
from sqlalchemy import or_

from . import db
from .models import Client, Instrument, InstrumentModel, User, UserRole, Part, App, ServiceType, MachineType, NAV_ACCESS_OPTIONS


admin_bp = Blueprint("admin", __name__, template_folder="templates")


def admin_required():
    if not current_user.is_authenticated or current_user.role != UserRole.ADMIN:
        abort(403)


@admin_bp.before_request
def check_admin():
    admin_required()


def _parse_decimal(value):
    if value is None:
        return None


def _nav_access_from_form():
    return request.form.getlist("nav_permissions")


def _client_user_role_from_form():
    role_raw = (request.form.get("role") or UserRole.CLIENT.value).strip().lower()
    return UserRole.CLIENT_ADMIN if role_raw == UserRole.CLIENT_ADMIN.value else UserRole.CLIENT


def _parse_optional_date(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _render_user_form(template, **context):
    context.setdefault("nav_access_options", NAV_ACCESS_OPTIONS)
    return render_template(template, **context)
    value = (value or "").strip()
    if not value:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


# Clients CRUD
@admin_bp.route("/clients")
@login_required
def clients_index():
    sales_users = User.query.filter(User.role == UserRole.SALES).order_by(User.full_name.asc()).all()

    name = (request.args.get("name") or "").strip()
    code = (request.args.get("code") or "").strip()
    sales_id = request.args.get("sales_id") or ""

    query = Client.query
    if name:
        query = query.filter(or_(Client.name.ilike(f"%{name}%"), Client.contact_person.ilike(f"%{name}%")))
    if code:
        query = query.filter(Client.client_code.ilike(f"%{code}%"))
    if sales_id:
        try:
            query = query.filter(Client.assigned_sales_id == int(sales_id))
        except ValueError:
            query = query.filter(False)  # invalid id yields no results

    clients = query.order_by(Client.name.asc()).all()
    client_users = {
        c.id: next((u for u in c.users if u.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)), None)
        for c in clients
    }
    selected_filters = {"name": name, "code": code, "sales_id": sales_id}
    return render_template(
        "admin/clients/index.html",
        clients=clients,
        client_users=client_users,
        sales_users=sales_users,
        selected_filters=selected_filters,
    )


@admin_bp.route("/clients/new", methods=["GET", "POST"])
@login_required
def clients_create():
    apps = App.query.order_by(App.name.asc()).all()
    sales_users = User.query.filter(User.role == UserRole.SALES).order_by(User.full_name.asc()).all()
    if request.method == "POST":
        name = request.form.get("name")
        client_code = request.form.get("client_code")
        address = request.form.get("address")
        contact_person = request.form.get("contact_person")
        contact_number = request.form.get("contact_number")
        assigned_sales_id = request.form.get("assigned_sales_id") or None
        email = request.form.get("email")
        app_ids = request.form.getlist("app_ids")

        errors = []

        if not name:
            errors.append("Name is required.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/clients/form.html", apps=apps, client_users=[])

        client = Client(
            name=name,
            client_code=client_code,
            address=address,
            contact_person=contact_person,
            contact_number=contact_number,
            assigned_sales_id=assigned_sales_id,
            email=email,
        )

        if app_ids:
            selected_apps = App.query.filter(App.id.in_(app_ids)).all()
            client.apps = selected_apps

        db.session.add(client)
        db.session.commit()
        flash("Client created.", "success")
        return redirect(url_for("admin.clients_index"))

    return render_template("admin/clients/form.html", apps=apps, client_users=[], sales_users=sales_users)


@admin_bp.route("/clients/<int:client_id>/edit", methods=["GET", "POST"])
@login_required
def clients_edit(client_id):
    client = Client.query.get_or_404(client_id)
    apps = App.query.order_by(App.name.asc()).all()
    sales_users = User.query.filter(User.role == UserRole.SALES).order_by(User.full_name.asc()).all()
    instruments = Instrument.query.filter(Instrument.client_id == client.id).order_by(Instrument.name.asc()).all()
    models = InstrumentModel.query.order_by(InstrumentModel.name.asc()).all()
    if request.method == "POST":
        if request.form.get("update_instrument") == "1":
            instrument_id_raw = (request.form.get("id") or "").strip()
            code = (request.form.get("code") or "").strip()
            name = (request.form.get("name") or "").strip()
            model_id_raw = (request.form.get("model_id") or "").strip()
            serial_number = (request.form.get("serial_number") or "").strip()
            installation_date = _parse_optional_date(request.form.get("installation_date"))
            warranty_end_date = _parse_optional_date(request.form.get("warranty_end_date"))

            errors = []
            if not code:
                errors.append("Instrument code is required.")
            if not name:
                errors.append("Instrument name is required.")

            model_id = None
            if model_id_raw:
                try:
                    model_id = int(model_id_raw)
                except ValueError:
                    errors.append("Instrument model is invalid.")

            instrument = None
            if instrument_id_raw:
                try:
                    instrument = Instrument.query.filter_by(id=int(instrument_id_raw), client_id=client.id).first()
                except ValueError:
                    instrument = None
                if instrument is None:
                    errors.append("Instrument was not found for this client.")

            duplicate_query = Instrument.query.filter(Instrument.code == code)
            if instrument is not None:
                duplicate_query = duplicate_query.filter(Instrument.id != instrument.id)
            if duplicate_query.first():
                errors.append("Instrument code already exists.")

            if errors:
                for msg in errors:
                    flash(msg, "danger")
                return render_template(
                    "admin/clients/form.html",
                    client=client,
                    client_users=[u for u in client.users if u.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)],
                    apps=apps,
                    sales_users=sales_users,
                    instruments=instruments,
                    models=models,
                )

            if instrument is None:
                instrument = Instrument(client_id=client.id, status="active")
                db.session.add(instrument)

            instrument.code = code
            instrument.name = name
            instrument.model_id = model_id
            instrument.serial_number = serial_number or None
            instrument.installation_date = installation_date
            instrument.warranty_end_date = warranty_end_date

            db.session.commit()
            flash("Instrument saved.", "success")
            return redirect(url_for("admin.clients_edit", client_id=client.id))

        client.name = request.form.get("name")
        client.client_code = request.form.get("client_code")
        client.address = request.form.get("address")
        client.contact_person = request.form.get("contact_person")
        client.contact_number = request.form.get("contact_number")
        client.assigned_sales_id = request.form.get("assigned_sales_id") or None
        client.email = request.form.get("email")
        app_ids = request.form.getlist("app_ids")
        selected_apps = App.query.filter(App.id.in_(app_ids)).all() if app_ids else []
        client.apps = selected_apps

        if not client.name:
            flash("Name is required.", "danger")
            return render_template(
                "admin/clients/form.html",
                client=client,
                client_users=[u for u in client.users if u.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)],
                apps=apps,
                sales_users=sales_users,
                instruments=instruments,
                models=models,
            )

        db.session.commit()
        flash("Client updated.", "success")
        return redirect(url_for("admin.clients_index"))

    return render_template(
        "admin/clients/form.html",
        client=client,
        client_users=[u for u in client.users if u.role in (UserRole.CLIENT, UserRole.CLIENT_ADMIN)],
        apps=apps,
        sales_users=sales_users,
        instruments=instruments,
        models=models,
    )


@admin_bp.route("/clients/<int:client_id>/delete", methods=["POST"])
@login_required
def clients_delete(client_id):
    client = Client.query.get_or_404(client_id)
    db.session.delete(client)
    db.session.commit()
    flash("Client deleted.", "info")
    return redirect(url_for("admin.clients_index"))


# Client user management
@admin_bp.route("/clients/<int:client_id>/users", methods=["POST"])
@login_required
def clients_add_user(client_id):
    client = Client.query.get_or_404(client_id)
    username = (request.form.get("username") or "").strip()
    full_name = request.form.get("full_name")
    contact_number = (request.form.get("contact_number") or "").strip()
    password = (request.form.get("password") or "").strip()
    role = _client_user_role_from_form()

    errors = []
    if not username:
        errors.append("Username is required.")
    if not password:
        errors.append("Password is required.")
    existing = User.query.filter_by(username=username).first()
    if existing:
        errors.append("Username already exists.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        return redirect(url_for("admin.clients_edit", client_id=client.id))

    user = User(
        username=username,
        full_name=full_name,
        contact_number=contact_number,
        role=role,
        client_id=client.id,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    flash("Client user added.", "success")
    return redirect(url_for("admin.clients_edit", client_id=client.id))

@admin_bp.route("/clients/<int:client_id>/users/<int:user_id>/edit", methods=["POST"])
@login_required
def clients_update_user(client_id, user_id):
    client = Client.query.get_or_404(client_id)
    user = User.query.get_or_404(user_id)
    if user.role not in (UserRole.CLIENT, UserRole.CLIENT_ADMIN) or user.client_id != client.id:
        abort(404)

    username = (request.form.get("username") or "").strip()
    full_name = request.form.get("full_name")
    contact_number = (request.form.get("contact_number") or "").strip()
    new_password = (request.form.get("password") or "").strip()
    role = _client_user_role_from_form()

    errors = []
    if not username:
        errors.append("Username is required.")
    existing = User.query.filter(User.username == username, User.id != user.id).first()
    if existing:
        errors.append("Username already exists.")

    if errors:
        for msg in errors:
            flash(msg, "danger")
        return redirect(url_for("admin.clients_edit", client_id=client.id))

    user.username = username
    user.full_name = full_name
    user.contact_number = contact_number
    user.role = role
    if new_password:
        user.set_password(new_password)
    db.session.commit()
    flash("Client user updated.", "success")
    return redirect(url_for("admin.clients_edit", client_id=client.id))


@admin_bp.route("/clients/<int:client_id>/users/<int:user_id>/delete", methods=["POST"])
@login_required
def clients_delete_user(client_id, user_id):
    client = Client.query.get_or_404(client_id)
    user = User.query.get_or_404(user_id)
    if user.role not in (UserRole.CLIENT, UserRole.CLIENT_ADMIN) or user.client_id != client.id:
        abort(404)
    db.session.delete(user)
    db.session.commit()
    flash("Client user deleted.", "info")
    return redirect(url_for("admin.clients_edit", client_id=client.id))


# Instruments CRUD
@admin_bp.route("/instruments")
@login_required
def instruments_index():
    clients = Client.query.order_by(Client.name.asc()).all()
    models = InstrumentModel.query.order_by(InstrumentModel.name.asc()).all()

    client_id = request.args.get("client_id") or ""
    name = (request.args.get("name") or "").strip()
    serial_number = (request.args.get("serial_number") or "").strip()
    model_id = request.args.get("model_id") or ""

    query = Instrument.query
    if client_id:
        try:
            query = query.filter(Instrument.client_id == int(client_id))
        except ValueError:
            query = query.filter(False)
    if name:
        query = query.filter(or_(Instrument.name.ilike(f"%{name}%"), Instrument.code.ilike(f"%{name}%")))
    if serial_number:
        query = query.filter(Instrument.serial_number.ilike(f"%{serial_number}%"))
    if model_id:
        try:
            query = query.filter(Instrument.model_id == int(model_id))
        except ValueError:
            query = query.filter(False)

    instruments = query.order_by(Instrument.name.asc()).all()
    selected_filters = {
        "client_id": client_id,
        "name": name,
        "serial_number": serial_number,
        "model_id": model_id,
    }
    return render_template(
        "admin/instruments/index.html",
        instruments=instruments,
        clients=clients,
        models=models,
        selected_filters=selected_filters,
    )


@admin_bp.route("/instruments/new", methods=["GET", "POST"])
@login_required
def instruments_create():
    clients = Client.query.order_by(Client.name.asc()).all()
    models = InstrumentModel.query.order_by(InstrumentModel.name.asc()).all()
    if request.method == "POST":
        code = request.form.get("code")
        name = request.form.get("name")
        client_id = request.form.get("client_id")
        model_id = request.form.get("model_id") or None
        serial_number = request.form.get("serial_number")
        status = request.form.get("status") or "active"
        installation_date_raw = (request.form.get("installation_date") or "").strip()
        warranty_end_raw = (request.form.get("warranty_end_date") or "").strip()

        def parse_date(val):
            try:
                return datetime.strptime(val, "%Y-%m-%d").date()
            except ValueError:
                return None

        installation_date = parse_date(installation_date_raw) if installation_date_raw else None
        warranty_end_date = parse_date(warranty_end_raw) if warranty_end_raw else None

        if not (code and name and client_id):
            flash("Code, name, client are required.", "danger")
            return render_template("admin/instruments/form.html", clients=clients, models=models)

        parsed_model_id = None
        if model_id:
            try:
                parsed_model_id = int(model_id)
            except ValueError:
                flash("Model is invalid.", "danger")
                return render_template("admin/instruments/form.html", clients=clients, models=models)

        instrument = Instrument(
            code=code,
            name=name,
            client_id=client_id,
            model_id=parsed_model_id,
            serial_number=serial_number,
            status=status,
            installation_date=installation_date,
            warranty_end_date=warranty_end_date,
        )
        db.session.add(instrument)
        db.session.commit()
        flash("Instrument created.", "success")
        return redirect(url_for("admin.instruments_index"))

    return render_template("admin/instruments/form.html", clients=clients, models=models)


@admin_bp.route("/instruments/<int:instrument_id>/edit", methods=["GET", "POST"])
@login_required
def instruments_edit(instrument_id):
    instrument = Instrument.query.get_or_404(instrument_id)
    clients = Client.query.order_by(Client.name.asc()).all()
    models = InstrumentModel.query.order_by(InstrumentModel.name.asc()).all()
    if request.method == "POST":
        instrument.code = request.form.get("code")
        instrument.name = request.form.get("name")
        instrument.client_id = request.form.get("client_id")
        model_id = request.form.get("model_id") or None
        if model_id:
            try:
                instrument.model_id = int(model_id)
            except ValueError:
                flash("Model is invalid.", "danger")
                return render_template("admin/instruments/form.html", instrument=instrument, clients=clients, models=models, machine_type=MachineType)
        else:
            instrument.model_id = None
        instrument.serial_number = request.form.get("serial_number")
        instrument.status = request.form.get("status") or "active"
        installation_date_raw = (request.form.get("installation_date") or "").strip()
        warranty_end_raw = (request.form.get("warranty_end_date") or "").strip()

        def parse_date(val):
            try:
                return datetime.strptime(val, "%Y-%m-%d").date()
            except ValueError:
                return None

        if installation_date_raw:
            instrument.installation_date = parse_date(installation_date_raw)
        # keep existing if left blank
        if warranty_end_raw:
            instrument.warranty_end_date = parse_date(warranty_end_raw)

        if not (instrument.code and instrument.name and instrument.client_id):
            flash("Code, name, client are required.", "danger")
            return render_template("admin/instruments/form.html", instrument=instrument, clients=clients, models=models, machine_type=MachineType)

        db.session.commit()
        flash("Instrument updated.", "success")
        return redirect(url_for("admin.instruments_index"))

    return render_template("admin/instruments/form.html", instrument=instrument, clients=clients, models=models, machine_type=MachineType)


@admin_bp.route("/instruments/<int:instrument_id>/delete", methods=["POST"])
@login_required
def instruments_delete(instrument_id):
    instrument = Instrument.query.get_or_404(instrument_id)
    db.session.delete(instrument)
    db.session.commit()
    flash("Instrument deleted.", "info")
    return redirect(url_for("admin.instruments_index"))


# Engineers (Users with ENGINEER role)
@admin_bp.route("/engineers")
@login_required
def engineers_index():
    engineers = User.query.filter_by(role=UserRole.ENGINEER).order_by(User.full_name.asc()).all()
    clients = Client.query.order_by(Client.name.asc()).all()
    return render_template("admin/engineers/index.html", engineers=engineers, clients=clients)


@admin_bp.route("/engineers/new", methods=["GET", "POST"])
@login_required
def engineers_create():
    clients = Client.query.order_by(Client.name.asc()).all()
    if request.method == "POST":
        username = request.form.get("username")
        full_name = request.form.get("full_name")
        password = request.form.get("password")
        client_id = request.form.get("client_id") or None
        user_type = request.form.get("user_type") or "Engineer"

        if not (username and password):
            flash("Username and password are required.", "danger")
            return _render_user_form("admin/engineers/form.html", clients=clients)

        existing = User.query.filter_by(username=username).first()
        if existing:
            flash("Username already exists.", "danger")
            return _render_user_form("admin/engineers/form.html", clients=clients)

        user = User(
            username=username,
            full_name=full_name,
            role=UserRole.ENGINEER,
            client_id=client_id,
            user_type=user_type,
        )
        user.set_nav_permissions(_nav_access_from_form())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Engineer created.", "success")
        return redirect(url_for("admin.engineers_index"))

    return _render_user_form("admin/engineers/form.html", clients=clients)


@admin_bp.route("/engineers/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def engineers_edit(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != UserRole.ENGINEER:
        abort(404)
    clients = Client.query.order_by(Client.name.asc()).all()

    if request.method == "POST":
        user.username = request.form.get("username")
        user.full_name = request.form.get("full_name")
        client_id = request.form.get("client_id") or None
        user.client_id = client_id
        new_password = request.form.get("password")
        user.user_type = request.form.get("user_type") or "Engineer"

        if new_password:
            user.set_password(new_password)

        if not user.username:
            flash("Username is required.", "danger")
            return _render_user_form("admin/engineers/form.html", engineer=user, clients=clients)

        user.set_nav_permissions(_nav_access_from_form())

        db.session.commit()
        flash("Engineer updated.", "success")
        return redirect(url_for("admin.engineers_index"))

    return _render_user_form("admin/engineers/form.html", engineer=user, clients=clients)


@admin_bp.route("/engineers/<int:user_id>/delete", methods=["POST"])
@login_required
def engineers_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != UserRole.ENGINEER:
        abort(404)
    db.session.delete(user)
    db.session.commit()
    flash("Engineer deleted.", "info")
    return redirect(url_for("admin.engineers_index"))


@admin_bp.route("/sales")
@login_required
def sales_index():
    sales_users = User.query.filter_by(role=UserRole.SALES).order_by(User.full_name.asc()).all()
    clients = Client.query.order_by(Client.name.asc()).all()
    return render_template("admin/sales_index.html", sales_users=sales_users, clients=clients)


@admin_bp.route("/sales/new", methods=["GET", "POST"])
@login_required
def sales_create():
    clients = Client.query.order_by(Client.name.asc()).all()
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = request.form.get("full_name")
        password = (request.form.get("password") or "").strip()
        client_id = request.form.get("client_id") or None

        errors = []
        if not username:
            errors.append("Username is required.")
        if not password:
            errors.append("Password is required.")
        if username:
            existing = User.query.filter_by(username=username).first()
            if existing:
                errors.append("Username already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return _render_user_form("admin/sales_form.html", clients=clients)

        user = User(
            username=username,
            full_name=full_name,
            role=UserRole.SALES,
            client_id=client_id,
            user_type="Sales",
        )
        user.set_nav_permissions(_nav_access_from_form())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Sales user created.", "success")
        return redirect(url_for("admin.sales_index"))

    return _render_user_form("admin/sales_form.html", clients=clients)


@admin_bp.route("/sales/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def sales_edit(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != UserRole.SALES:
        abort(404)
    clients = Client.query.order_by(Client.name.asc()).all()

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = request.form.get("full_name")
        client_id = request.form.get("client_id") or None
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
            return _render_user_form("admin/sales_form.html", sales_user=user, clients=clients)

        user.username = username
        user.full_name = full_name
        user.client_id = client_id
        user.user_type = "Sales"
        user.set_nav_permissions(_nav_access_from_form())
        if new_password:
            user.set_password(new_password)

        db.session.commit()
        flash("Sales user updated.", "success")
        return redirect(url_for("admin.sales_index"))

    return _render_user_form("admin/sales_form.html", sales_user=user, clients=clients)


@admin_bp.route("/sales/<int:user_id>/delete", methods=["POST"])
@login_required
def sales_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != UserRole.SALES:
        abort(404)
    db.session.delete(user)
    db.session.commit()
    flash("Sales user deleted.", "info")
    return redirect(url_for("admin.sales_index"))


# Admin Users CRUD
@admin_bp.route("/admins")
@login_required
def admins_index():
    admins = User.query.filter_by(role=UserRole.ADMIN).order_by(User.full_name.asc()).all()
    return render_template("admin/admins_index.html", admins=admins)


@admin_bp.route("/admins/new", methods=["GET", "POST"])
@login_required
def admins_create():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = request.form.get("full_name")
        password = (request.form.get("password") or "").strip()

        errors = []
        if not username:
            errors.append("Username is required.")
        if not password:
            errors.append("Password is required.")
        if username:
            existing = User.query.filter_by(username=username).first()
            if existing:
                errors.append("Username already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return _render_user_form("admin/admins_form.html")

        user = User(
            username=username,
            full_name=full_name,
            role=UserRole.ADMIN,
            is_active_user=True,
            user_type="Administrator",
        )
        user.set_nav_permissions(_nav_access_from_form())
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        flash("Admin user created.", "success")
        return redirect(url_for("admin.admins_index"))

    return _render_user_form("admin/admins_form.html")


@admin_bp.route("/admins/<int:user_id>/edit", methods=["GET", "POST"])
@login_required
def admins_edit(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != UserRole.ADMIN:
        abort(404)

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        full_name = request.form.get("full_name")
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
            return _render_user_form("admin/admins_form.html", admin_user=user)

        user.username = username
        user.full_name = full_name
        user.set_nav_permissions(_nav_access_from_form())
        if new_password:
            user.set_password(new_password)

        db.session.commit()
        flash("Admin user updated.", "success")
        return redirect(url_for("admin.admins_index"))

    return _render_user_form("admin/admins_form.html", admin_user=user)


@admin_bp.route("/admins/<int:user_id>/delete", methods=["POST"])
@login_required
def admins_delete(user_id):
    user = User.query.get_or_404(user_id)
    if user.role != UserRole.ADMIN:
        abort(404)
    if user.id == current_user.id:
        flash("You cannot delete your own admin account.", "danger")
        return redirect(url_for("admin.admins_index"))
    db.session.delete(user)
    db.session.commit()
    flash("Admin user deleted.", "info")
    return redirect(url_for("admin.admins_index"))


@admin_bp.route("/system-setup", methods=["GET", "POST"])
@login_required
def system_setup():
    users = User.query.order_by(User.role.asc(), User.full_name.asc(), User.username.asc()).all()

    if request.method == "POST":
        for user in users:
            user.set_nav_permissions(request.form.getlist(f"nav_permissions_{user.id}"))
        db.session.commit()
        flash("System setup updated.", "success")
        return redirect(url_for("admin.system_setup"))

    return render_template(
        "admin/system_setup.html",
        users=users,
        nav_access_options=NAV_ACCESS_OPTIONS,
    )


# Parts CRUD
@admin_bp.route("/parts")
@login_required
def parts_index():
    parts = Part.query.order_by(Part.name.asc()).all()
    return render_template("admin/parts/index.html", parts=parts)


@admin_bp.route("/parts/new", methods=["GET", "POST"])
@login_required
def parts_create():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = request.form.get("description")
        unit_cost_raw = request.form.get("unit_cost")
        price_raw = request.form.get("price")
        unit_cost = _parse_decimal(unit_cost_raw)
        price = _parse_decimal(price_raw)
        errors = []
        if unit_cost_raw and unit_cost is None:
            errors.append("Unit cost must be a valid number.")
        if price_raw and price is None:
            errors.append("Price must be a valid number.")
        if not name:
            errors.append("Name is required.")
        existing = Part.query.filter_by(name=name).first()
        if existing:
            errors.append("Part already exists.")
        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/parts/form.html")
        part = Part(name=name, description=description, unit_cost=unit_cost, price=price)
        db.session.add(part)
        db.session.commit()
        flash("Part created.", "success")
        return redirect(url_for("admin.parts_index"))
    return render_template("admin/parts/form.html")


@admin_bp.route("/parts/<int:part_id>/edit", methods=["GET", "POST"])
@login_required
def parts_edit(part_id):
    part = Part.query.get_or_404(part_id)
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        description = request.form.get("description")
        unit_cost_raw = request.form.get("unit_cost")
        price_raw = request.form.get("price")
        unit_cost = _parse_decimal(unit_cost_raw)
        price = _parse_decimal(price_raw)
        errors = []
        if not name:
            errors.append("Name is required.")
        existing = Part.query.filter(Part.id != part.id, Part.name == name).first()
        if existing:
            errors.append("Part name already used.")
        if unit_cost_raw and unit_cost is None:
            errors.append("Unit cost must be a valid number.")
        if price_raw and price is None:
            errors.append("Price must be a valid number.")
        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/parts/form.html", part=part)
        part.name = name
        part.description = description
        part.unit_cost = unit_cost
        part.price = price
        db.session.commit()
        flash("Part updated.", "success")
        return redirect(url_for("admin.parts_index"))
    return render_template("admin/parts/form.html", part=part)


@admin_bp.route("/parts/<int:part_id>/delete", methods=["POST"])
@login_required
def parts_delete(part_id):
    part = Part.query.get_or_404(part_id)
    db.session.delete(part)
    db.session.commit()
    flash("Part deleted.", "info")
    return redirect(url_for("admin.parts_index"))


# Apps (CDS Application) CRUD
@admin_bp.route("/apps")
@login_required
def apps_index():
    apps = App.query.order_by(App.name.asc()).all()
    return render_template("admin/apps/index.html", apps=apps)


@admin_bp.route("/apps/new", methods=["GET", "POST"])
@login_required
def apps_create():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        description = request.form.get("description")

        errors = []
        if not code:
            errors.append("Code is required.")
        if not name:
            errors.append("Name is required.")
        existing = App.query.filter_by(code=code).first()
        if existing:
            errors.append("Code already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/apps/form.html", app=None)

        app_obj = App(code=code, name=name, description=description)
        db.session.add(app_obj)
        db.session.commit()
        flash("CDS Application created.", "success")
        return redirect(url_for("admin.apps_index"))

    return render_template("admin/apps/form.html", app=None)


@admin_bp.route("/apps/<int:app_id>/edit", methods=["GET", "POST"])
@login_required
def apps_edit(app_id):
    app_obj = App.query.get_or_404(app_id)
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        description = request.form.get("description")

        errors = []
        if not code:
            errors.append("Code is required.")
        if not name:
            errors.append("Name is required.")
        existing = App.query.filter(App.id != app_obj.id, App.code == code).first()
        if existing:
            errors.append("Code already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/apps/form.html", app=app_obj)

        app_obj.code = code
        app_obj.name = name
        app_obj.description = description
        db.session.commit()
        flash("CDS Application updated.", "success")
        return redirect(url_for("admin.apps_index"))

    return render_template("admin/apps/form.html", app=app_obj)


@admin_bp.route("/apps/<int:app_id>/delete", methods=["POST"])
@login_required
def apps_delete(app_id):
    app_obj = App.query.get_or_404(app_id)
    db.session.delete(app_obj)
    db.session.commit()
    flash("CDS Application deleted.", "info")
    return redirect(url_for("admin.apps_index"))


# Service Types CRUD
@admin_bp.route("/service-types")
@login_required
def service_types_index():
    types = ServiceType.query.order_by(ServiceType.name.asc()).all()
    return render_template("admin/service_types/index.html", types=types)


@admin_bp.route("/service-types/new", methods=["GET", "POST"])
@login_required
def service_types_create():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        description = request.form.get("description")

        errors = []
        if not code:
            errors.append("Code is required.")
        if not name:
            errors.append("Name is required.")
        existing = ServiceType.query.filter_by(code=code).first()
        if existing:
            errors.append("Code already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/service_types/form.html")

        svc = ServiceType(code=code, name=name, description=description)
        db.session.add(svc)
        db.session.commit()
        flash("Service type created.", "success")
        return redirect(url_for("admin.service_types_index"))

    return render_template("admin/service_types/form.html")


@admin_bp.route("/service-types/<int:type_id>/edit", methods=["GET", "POST"])
@login_required
def service_types_edit(type_id):
    svc = ServiceType.query.get_or_404(type_id)
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        description = request.form.get("description")

        errors = []
        if not code:
            errors.append("Code is required.")
        if not name:
            errors.append("Name is required.")
        existing = ServiceType.query.filter(ServiceType.id != svc.id, ServiceType.code == code).first()
        if existing:
            errors.append("Code already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/service_types/form.html", svc=svc)

        svc.code = code
        svc.name = name
        svc.description = description
        db.session.commit()
        flash("Service type updated.", "success")
        return redirect(url_for("admin.service_types_index"))

    return render_template("admin/service_types/form.html", svc=svc)


@admin_bp.route("/service-types/<int:type_id>/delete", methods=["POST"])
@login_required
def service_types_delete(type_id):
    svc = ServiceType.query.get_or_404(type_id)
    db.session.delete(svc)
    db.session.commit()
    flash("Service type deleted.", "info")
    return redirect(url_for("admin.service_types_index"))


# Instrument Models CRUD
@admin_bp.route("/instruments_model")
@login_required
def instrument_models_index():
    q = (request.args.get("q") or "").strip()
    query = InstrumentModel.query
    if q:
        query = query.filter(or_(InstrumentModel.code.ilike(f"%{q}%"), InstrumentModel.name.ilike(f"%{q}%"), InstrumentModel.brand_name.ilike(f"%{q}%")))
    models = query.order_by(InstrumentModel.name.asc()).all()
    return render_template("admin/instrument_model/index.html", models=models, q=q)


@admin_bp.route("/instruments_model/new", methods=["GET", "POST"])
@login_required
def instrument_models_create():
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        brand_name = (request.form.get("brand_name") or "").strip()

        errors = []
        if not code:
            errors.append("Code is required.")
        if not name:
            errors.append("Name is required.")
        if not brand_name:
            errors.append("Brand is required.")
        if code:
            existing = InstrumentModel.query.filter_by(code=code).first()
            if existing:
                errors.append("Code already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            return render_template("admin/instrument_model/form.html", model=None)

        model = InstrumentModel(code=code, name=name, brand_name=brand_name)
        db.session.add(model)
        db.session.commit()
        flash("Instrument model created.", "success")
        return redirect(url_for("admin.instrument_models_index"))

    return render_template("admin/instrument_model/form.html", model=None, machine_type=MachineType)


@admin_bp.route("/instruments_model/<int:model_id>/edit", methods=["GET", "POST"])
@login_required
def instrument_models_edit(model_id):
    model = InstrumentModel.query.get_or_404(model_id)
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        name = (request.form.get("name") or "").strip()
        brand_name = (request.form.get("brand_name") or "").strip()
        machine_type = (request.form.get("machine_type") or "").strip()

        errors = []
        if not code:
            errors.append("Code is required.")
        if not name:
            errors.append("Name is required.")
        if not brand_name:
            errors.append("Brand is required.")
        existing = InstrumentModel.query.filter(InstrumentModel.id != model.id, InstrumentModel.code == code).first()
        if existing:
            errors.append("Code already exists.")

        if errors:
            for msg in errors:
                flash(msg, "danger")
            model.code = code
            model.name = name
            model.brand_name = brand_name
            model.machine_type = machine_type
            return render_template("admin/instrument_model/form.html", model=model, machine_type=MachineType)

        model.code = code
        model.name = name
        model.brand_name = brand_name
        model.machine_type = machine_type
        db.session.commit()
        flash("Instrument model updated.", "success")
        return redirect(url_for("admin.instrument_models_index"))

    return render_template("admin/instrument_model/form.html", model=model, machine_type=MachineType)


@admin_bp.route("/instruments_model/<int:model_id>/delete", methods=["POST"])
@login_required
def instrument_models_delete(model_id):
    model = InstrumentModel.query.get_or_404(model_id)
    in_use = Instrument.query.filter(Instrument.model_id == model.id).first()
    if in_use:
        flash("Cannot delete: model is assigned to one or more instruments.", "danger")
        return redirect(url_for("admin.instrument_models_index"))

    db.session.delete(model)
    db.session.commit()
    flash("Instrument model deleted.", "info")
    return redirect(url_for("admin.instrument_models_index"))
