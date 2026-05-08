from datetime import datetime, timedelta

from flask import Blueprint, render_template
from flask_login import login_required, current_user

from . import db
from .models import (
    Client,
    Instrument,
    PreventiveMaintenanceSchedule,
    ServiceLog,
    Ticket,
    TicketStatus,
    UserRole,
)
TASK_CATEGORY_PREFIX = "task:"


def _exclude_task_tickets(query):
    return query.filter(db.or_(Ticket.category.is_(None), ~Ticket.category.like(f"{TASK_CATEGORY_PREFIX}%")))


def _apply_client_ticket_scope(query):
    if current_user.role == UserRole.CLIENT:
        return query.filter(
            Ticket.client_id == current_user.client_id,
            Ticket.reported_by_id == current_user.id,
        )
    if current_user.role == UserRole.CLIENT_ADMIN:
        return query.filter(Ticket.client_id == current_user.client_id)
    return query

main_bp = Blueprint(
        "main", __name__, 
        template_folder="templates",
        static_folder="static")

@main_bp.route("/user-manual")
def user_manual():
    return render_template("user_manual.html")

@main_bp.route("/")
@login_required
def dashboard():
    # Global counts (overridden for clients below)
    open_like_statuses = [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.REOPENED]
    total_tickets = Ticket.query.count()
    open_tickets = Ticket.query.filter(Ticket.status.in_(open_like_statuses)).count()
    total_instruments = Instrument.query.count()

    role = current_user.role
    if role == UserRole.SALES:
        total_tickets = _exclude_task_tickets(Ticket.query).count()
        open_tickets = _exclude_task_tickets(Ticket.query).filter(Ticket.status.in_(open_like_statuses)).count()

    ticket_scope = Ticket.query
    service_log_scope = ServiceLog.query
    if role == UserRole.ENGINEER:
        ticket_scope = ticket_scope.filter(Ticket.assigned_engineer_id == current_user.id)
        service_log_scope = service_log_scope.filter(ServiceLog.engineer_id == current_user.id)
        total_tickets = ticket_scope.count()
        open_tickets = ticket_scope.filter(Ticket.status.in_(open_like_statuses)).count()

    engineer_stats = {}
    client_stats = {}
    admin_data = {}
    sales_data = {}
    client_dash = {}

    today = datetime.utcnow().date()
    week_ahead = today + timedelta(days=7)
    year_start = today.replace(month=1, day=1)
    year_end = today.replace(month=12, day=31)
    week_range = {"start": today.strftime("%Y-%m-%d"), "end": week_ahead.strftime("%Y-%m-%d")}

    # Engineer dashboard data
    if role == UserRole.ENGINEER:
        my_assigned_open = Ticket.query.filter(
            Ticket.assigned_engineer_id == current_user.id,
            Ticket.status.in_(open_like_statuses),
        ).count()

        my_reported_open = Ticket.query.filter(
            Ticket.reported_by_id == current_user.id,
            Ticket.status.in_(open_like_statuses),
        ).count()

        my_recent_tickets = Ticket.query.filter(
            Ticket.assigned_engineer_id == current_user.id
        ).order_by(Ticket.created_at.desc()).limit(8).all()

        my_recent_logs = ServiceLog.query.filter(
            ServiceLog.engineer_id == current_user.id
        ).order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc()).limit(5).all()

        engineer_stats = {
            "my_assigned_open": my_assigned_open,
            "my_reported_open": my_reported_open,
            "my_recent_tickets": my_recent_tickets,
            "my_recent_logs": my_recent_logs,
        }

    # Admin / Engineer shared dashboard data
    if role in [UserRole.ADMIN, UserRole.ENGINEER]:
        pm_scope = PreventiveMaintenanceSchedule.query
        if role == UserRole.ENGINEER:
            pm_scope = pm_scope.filter(PreventiveMaintenanceSchedule.assigned_engineer_id == current_user.id)

        pm_week = (
            pm_scope.filter(
                PreventiveMaintenanceSchedule.date >= today,
                PreventiveMaintenanceSchedule.date <= week_ahead,
            )
            .order_by(PreventiveMaintenanceSchedule.date.asc())
            .all()
        )
        ticket_deadlines = (
            ticket_scope.filter(
                Ticket.target_date.isnot(None),
                Ticket.target_date <= week_ahead,
            )
            .order_by(Ticket.target_date.asc())
            .limit(15)
            .all()
        )
        ticket_overview = ticket_scope.order_by(Ticket.created_at.desc()).limit(20).all()
        logs_week = (
            service_log_scope.filter(
                ServiceLog.visit_date >= today,
                ServiceLog.visit_date <= week_ahead,
            )
            .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
            .limit(12)
            .all()
        )
        pm_total = pm_scope.filter(
            PreventiveMaintenanceSchedule.date >= year_start,
            PreventiveMaintenanceSchedule.date <= year_end,
        ).count()
        logs_year = service_log_scope.filter(
            ServiceLog.visit_date >= year_start,
            ServiceLog.visit_date <= year_end,
        ).all()

        def count_type(prefix: str):
            total = onsite = remote = 0
            for log in logs_year:
                st = (log.service_type or "").lower()
                if not st.startswith(prefix):
                    continue
                total += 1
                if "onsite" in st:
                    onsite += 1
                elif "remote" in st:
                    remote += 1
            return {"total": total, "onsite": onsite, "remote": remote}

        def count_specific(prefix: str):
            return len([log for log in logs_year if (log.service_type or "").lower().startswith(prefix)])

        service_log_stats = {
            "corrective": count_type("corrective"),
            "calibration": count_type("calibration"),
            "pm": count_type("pm"),
            "install_pullout": {
                "total": count_specific("install") + count_specific("pullout"),
                "installation": count_specific("install"),
                "pullout": count_specific("pullout"),
            },
            "demo_endorsement": {
                "total": count_specific("demo") + count_specific("endorsement"),
                "demo": count_specific("demo"),
                "endorsement": count_specific("endorsement"),
            },
        }

        admin_data = {
            "pm_week": pm_week,
            "ticket_deadlines": ticket_deadlines,
            "ticket_overview": ticket_overview,
            "logs_week": logs_week,
            "pm_total": pm_total,
            "service_log_stats": service_log_stats,
            "week_range": week_range,
            "current_year": today.year,
        }

    # Client dashboard data
    if role in [UserRole.CLIENT, UserRole.CLIENT_ADMIN] and current_user.client_id:
        my_client = Client.query.get(current_user.client_id)
        if my_client:
            base_q = _apply_client_ticket_scope(_exclude_task_tickets(Ticket.query))
            my_total_tickets = base_q.count()
            my_open_tickets = base_q.filter(Ticket.status.in_(open_like_statuses)).count()
            my_closed_tickets = base_q.filter(
                Ticket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED])
            ).count()

            my_recent_tickets = base_q.order_by(Ticket.created_at.desc()).limit(8).all()

            last_service = ServiceLog.query.filter(
                ServiceLog.client_id == my_client.id
            ).order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc()).first()

            # Override global KPIs so clients only see their own data
            total_tickets = my_total_tickets
            open_tickets = my_open_tickets
            total_instruments = Instrument.query.filter(Instrument.client_id == my_client.id).count()
        else:
            my_total_tickets = my_open_tickets = my_closed_tickets = 0
            my_recent_tickets = []
            last_service = None
            total_tickets = open_tickets = total_instruments = 0

        client_stats = {
            "client": my_client,
            "my_total_tickets": my_total_tickets,
            "my_open_tickets": my_open_tickets,
            "my_closed_tickets": my_closed_tickets,
            "my_recent_tickets": my_recent_tickets,
            "last_service": last_service,
        }
        client_dash = {
            "week_tickets": base_q.filter(
                Ticket.target_date.isnot(None),
                Ticket.target_date <= week_ahead,
            )
            .order_by(Ticket.target_date.asc())
            .limit(15)
            .all(),
            "timeline_tickets": base_q.order_by(
                Ticket.target_date.is_(None),  # put nulls last in MySQL by sorting boolean first
                Ticket.target_date.asc(),
                Ticket.created_at.desc(),
            ).limit(25).all(),
            "logs": ServiceLog.query.filter(ServiceLog.client_id == my_client.id)
            .order_by(ServiceLog.visit_date.desc(), ServiceLog.id.desc())
            .limit(10)
            .all(),
        }
    else:
        client_dash = {"week_tickets": [], "timeline_tickets": [], "logs": []}

    # Sales dashboard data (scoped by assigned clients)
    if role == UserRole.SALES:
        sales_clients = Client.query.filter(Client.assigned_sales_id == current_user.id).all()
        client_ids = [c.id for c in sales_clients]
        if client_ids:
            ticket_q = _exclude_task_tickets(Ticket.query.filter(Ticket.client_id.in_(client_ids)))
            sales_tickets = ticket_q.order_by(Ticket.created_at.desc()).limit(25).all()
            week_sales_tickets = (
                ticket_q.filter(
                    Ticket.target_date.isnot(None),
                    Ticket.target_date <= week_ahead,
                )
                .order_by(Ticket.target_date.asc())
                .limit(15)
                .all()
            )
        else:
            sales_tickets = []
            week_sales_tickets = []

        def count_status(tickets, statuses):
            return len([t for t in tickets if t.status in statuses])

        active_statuses = [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.REOPENED]
        sales_summary = {
            "new": count_status(sales_tickets, [TicketStatus.OPEN]),
            "in_progress": count_status(sales_tickets, [TicketStatus.IN_PROGRESS]),
            "backlog": count_status(sales_tickets, [TicketStatus.ON_HOLD, TicketStatus.REOPENED]),
        }

        client_active_counts = []
        for c in sales_clients:
            count = (
                _exclude_task_tickets(Ticket.query.filter(
                    Ticket.client_id == c.id,
                    Ticket.status.in_(active_statuses),
                )).count()
                if client_ids
                else 0
            )
            client_active_counts.append({"client": c, "active_count": count})

        sales_data = {
            "clients": sales_clients,
            "week_tickets": week_sales_tickets,
            "summary": sales_summary,
            "client_active_counts": client_active_counts,
            "all_tickets": sales_tickets,
        }
    else:
        sales_data = {
            "clients": [],
            "week_tickets": [],
            "summary": {"new": 0, "in_progress": 0, "backlog": 0},
            "client_active_counts": [],
            "all_tickets": [],
        }

    return render_template(
        "dashboard.html",
        total_tickets=total_tickets,
        open_tickets=open_tickets,
        total_instruments=total_instruments,
        engineer_stats=engineer_stats,
        client_stats=client_stats,
        admin_data=admin_data,
        sales_data=sales_data,
        client_dash=client_dash,
    )
