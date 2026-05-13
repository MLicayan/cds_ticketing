from datetime import datetime, timedelta
import calendar

from flask import Blueprint, jsonify, render_template, request
from flask_login import login_required, current_user

from . import db
from .models import (
    Client,
    Instrument,
    PreventiveMaintenanceSchedule,
    ServiceLog,
    Ticket,
    TicketPriority,
    TicketStatus,
    User,
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


def _shift_month(month_date, offset):
    month_index = month_date.month - 1 + offset
    year = month_date.year + month_index // 12
    month = month_index % 12 + 1
    return month_date.replace(year=year, month=month, day=1)


def _parse_schedule_month(value, today):
    selected_month = today.replace(day=1)
    if value:
        try:
            selected_month = datetime.strptime(value.strip(), "%Y-%m").date().replace(day=1)
        except ValueError:
            selected_month = today.replace(day=1)
    return selected_month


def _dashboard_ticket_scope_for_user():
    role = current_user.role
    user_type = (current_user.user_type or "").strip().lower()
    can_view_overall = role == UserRole.ADMIN or (role == UserRole.ENGINEER and user_type == "support")
    scoped_client_ids = []
    ticket_scope = _exclude_task_tickets(Ticket.query)

    if role == UserRole.SALES:
        sales_clients = Client.query.filter(Client.assigned_sales_id == current_user.id).all()
        scoped_client_ids = [client.id for client in sales_clients]
        ticket_scope = ticket_scope.filter(Ticket.client_id.in_(scoped_client_ids)) if scoped_client_ids else ticket_scope.filter(db.false())
    elif role in [UserRole.CLIENT, UserRole.CLIENT_ADMIN]:
        ticket_scope = _apply_client_ticket_scope(ticket_scope)
    elif role == UserRole.ENGINEER and not can_view_overall:
        ticket_scope = ticket_scope.filter(Ticket.assigned_engineer_id == current_user.id)

    return ticket_scope


def _schedule_calendar_data(ticket_scope, selected_calendar_month, today):
    month_calendar = calendar.Calendar(firstweekday=0).monthdatescalendar(
        selected_calendar_month.year,
        selected_calendar_month.month,
    )
    next_calendar_month_start = _shift_month(selected_calendar_month, 1)
    selected_month_start_dt = datetime.combine(selected_calendar_month, datetime.min.time())
    next_month_start_dt = datetime.combine(next_calendar_month_start, datetime.min.time())
    calendar_deadlines = (
        ticket_scope.filter(
            db.or_(
                db.and_(
                    Ticket.target_date.isnot(None),
                    Ticket.target_date >= selected_calendar_month,
                    Ticket.target_date < next_calendar_month_start,
                ),
                db.and_(
                    Ticket.target_date.is_(None),
                    Ticket.date_needed.isnot(None),
                    Ticket.date_needed >= selected_month_start_dt,
                    Ticket.date_needed < next_month_start_dt,
                ),
            )
        )
        .all()
    )
    due_counts = {}
    for ticket in calendar_deadlines:
        due_date = ticket.target_date or (ticket.date_needed.date() if ticket.date_needed else None)
        if due_date:
            due_counts[due_date] = due_counts.get(due_date, 0) + 1

    return {
        "calendar_weeks": month_calendar,
        "calendar_month": selected_calendar_month.strftime("%B %Y"),
        "calendar_date": selected_calendar_month,
        "previous_calendar_month": _shift_month(selected_calendar_month, -1).strftime("%Y-%m"),
        "next_calendar_month": _shift_month(selected_calendar_month, 1).strftime("%Y-%m"),
        "is_current_calendar_month": selected_calendar_month == today.replace(day=1),
        "today": today,
        "due_counts": due_counts,
    }

main_bp = Blueprint(
        "main", __name__, 
        template_folder="templates",
        static_folder="static")

@main_bp.route("/user-manual")
def user_manual():
    return render_template("user_manual.html")


@main_bp.route("/dashboard/schedule-calendar")
@login_required
def schedule_calendar():
    today = datetime.utcnow().date()
    selected_calendar_month = _parse_schedule_month(request.args.get("schedule_month", ""), today)
    calendar_data = _schedule_calendar_data(
        _dashboard_ticket_scope_for_user(),
        selected_calendar_month,
        today,
    )
    return jsonify(
        {
            "html": render_template(
                "_dashboard_schedule_calendar.html",
                admin_data=calendar_data,
            ),
            "previous_month": calendar_data["previous_calendar_month"],
            "next_month": calendar_data["next_calendar_month"],
            "month": calendar_data["calendar_month"],
        }
    )

@main_bp.route("/")
@login_required
def dashboard():
    # Global counts (overridden for clients below)
    open_like_statuses = [TicketStatus.OPEN, TicketStatus.IN_PROGRESS, TicketStatus.ON_HOLD, TicketStatus.REOPENED]
    total_tickets = Ticket.query.count()
    open_tickets = Ticket.query.filter(Ticket.status.in_(open_like_statuses)).count()
    total_instruments = Instrument.query.count()

    role = current_user.role
    user_type = (current_user.user_type or "").strip().lower()
    can_view_overall = role == UserRole.ADMIN or (role == UserRole.ENGINEER and user_type == "support")

    scoped_client_ids = []
    ticket_scope = _exclude_task_tickets(Ticket.query)
    service_log_scope = ServiceLog.query
    if role == UserRole.SALES:
        sales_clients = Client.query.filter(Client.assigned_sales_id == current_user.id).all()
        scoped_client_ids = [client.id for client in sales_clients]
        ticket_scope = ticket_scope.filter(Ticket.client_id.in_(scoped_client_ids)) if scoped_client_ids else ticket_scope.filter(db.false())
        service_log_scope = service_log_scope.filter(ServiceLog.client_id.in_(scoped_client_ids)) if scoped_client_ids else service_log_scope.filter(db.false())
    elif role in [UserRole.CLIENT, UserRole.CLIENT_ADMIN]:
        ticket_scope = _apply_client_ticket_scope(ticket_scope)
        if current_user.client_id:
            scoped_client_ids = [current_user.client_id]
            service_log_scope = service_log_scope.filter(ServiceLog.client_id == current_user.client_id)
        else:
            service_log_scope = service_log_scope.filter(db.false())
    elif role == UserRole.ENGINEER and not can_view_overall:
        ticket_scope = ticket_scope.filter(Ticket.assigned_engineer_id == current_user.id)
        service_log_scope = service_log_scope.filter(ServiceLog.engineer_id == current_user.id)

    total_tickets = ticket_scope.count()
    open_tickets = ticket_scope.filter(Ticket.status.in_(open_like_statuses)).count()
    if role in [UserRole.CLIENT, UserRole.CLIENT_ADMIN] and current_user.client_id:
        total_instruments = Instrument.query.filter(Instrument.client_id == current_user.client_id).count()
    elif role == UserRole.SALES:
        total_instruments = Instrument.query.filter(Instrument.client_id.in_(scoped_client_ids)).count() if scoped_client_ids else 0

    engineer_stats = {}
    client_stats = {}
    admin_data = {}
    sales_data = {}
    client_dash = {}

    today = datetime.utcnow().date()
    selected_calendar_month = _parse_schedule_month(request.args.get("schedule_month", ""), today)
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

    # Shared dashboard data. Admin and support see overall; other roles see their own scope.
    if True:
        pm_scope = PreventiveMaintenanceSchedule.query
        if role == UserRole.ENGINEER and not can_view_overall:
            pm_scope = pm_scope.filter(PreventiveMaintenanceSchedule.assigned_engineer_id == current_user.id)
        elif role in [UserRole.CLIENT, UserRole.CLIENT_ADMIN] and current_user.client_id:
            pm_scope = pm_scope.filter(PreventiveMaintenanceSchedule.client_id == current_user.client_id)
        elif role == UserRole.SALES:
            pm_scope = pm_scope.filter(PreventiveMaintenanceSchedule.client_id.in_(scoped_client_ids)) if scoped_client_ids else pm_scope.filter(db.false())

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
        if role in [UserRole.CLIENT, UserRole.CLIENT_ADMIN]:
            active_tickets = ticket_scope.order_by(Ticket.created_at.desc()).limit(8).all()
        else:
            active_tickets = (
                ticket_scope.filter(Ticket.status.in_(open_like_statuses))
                .order_by(Ticket.date_needed.asc(), Ticket.target_date.asc(), Ticket.created_at.desc())
                .limit(8)
                .all()
            )
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

        all_scope = ticket_scope
        status_counts = {
            "open": all_scope.filter(Ticket.status == TicketStatus.OPEN).count(),
            "in_progress": all_scope.filter(Ticket.status == TicketStatus.IN_PROGRESS).count(),
            "resolved": all_scope.filter(Ticket.status == TicketStatus.RESOLVED).count(),
            "closed": all_scope.filter(Ticket.status == TicketStatus.CLOSED).count(),
            "overdue": all_scope.filter(
                Ticket.target_date.isnot(None),
                Ticket.target_date < today,
                Ticket.status.notin_([TicketStatus.RESOLVED, TicketStatus.CLOSED, TicketStatus.CANCELLED]),
            ).count(),
        }
        priority_counts = {
            "critical": all_scope.filter(Ticket.priority == TicketPriority.CRITICAL).count(),
            "high": all_scope.filter(Ticket.priority == TicketPriority.HIGH).count(),
            "medium": all_scope.filter(Ticket.priority == TicketPriority.MEDIUM).count(),
            "low": all_scope.filter(Ticket.priority == TicketPriority.LOW).count(),
        }

        calendar_data = _schedule_calendar_data(ticket_scope, selected_calendar_month, today)

        engineers_query = User.query.filter(
            User.role == UserRole.ENGINEER,
            User.is_active_user.is_(True),
        )
        if role == UserRole.ENGINEER and not can_view_overall:
            engineers_query = engineers_query.filter(User.id == current_user.id)
        elif not can_view_overall:
            scoped_engineer_ids = [
                row[0]
                for row in ticket_scope.with_entities(Ticket.assigned_engineer_id)
                .filter(Ticket.assigned_engineer_id.isnot(None))
                .distinct()
                .all()
            ]
            engineers_query = engineers_query.filter(User.id.in_(scoped_engineer_ids)) if scoped_engineer_ids else engineers_query.filter(db.false())
        engineers = engineers_query.order_by(User.full_name.asc(), User.username.asc()).limit(6).all()
        engineer_performance = []
        completed_statuses = [TicketStatus.RESOLVED, TicketStatus.CLOSED]
        performance_ticket_scope = Ticket.query if can_view_overall else ticket_scope
        for engineer in engineers:
            assigned_count = performance_ticket_scope.filter(Ticket.assigned_engineer_id == engineer.id).count()
            completed_count = performance_ticket_scope.filter(
                Ticket.assigned_engineer_id == engineer.id,
                Ticket.status.in_(completed_statuses),
            ).count()
            completion_rate = round((completed_count / assigned_count) * 100) if assigned_count else 0
            engineer_performance.append(
                {
                    "engineer": engineer,
                    "assigned": assigned_count,
                    "completed": completed_count,
                    "completion_rate": completion_rate,
                }
            )

        recent_activity = []
        for ticket in ticket_scope.order_by(Ticket.updated_at.desc()).limit(6).all():
            recent_activity.append(
                {
                    "kind": "ticket",
                    "title": f"Ticket {ticket.ticket_no} updated",
                    "description": ticket.subject,
                    "timestamp": ticket.updated_at or ticket.created_at,
                    "icon": "fa-ticket-alt",
                    "tone": "blue",
                }
            )
        for log in service_log_scope.order_by(ServiceLog.created_at.desc(), ServiceLog.id.desc()).limit(4).all():
            log_timestamp = log.created_at
            if not log_timestamp and log.visit_date:
                log_timestamp = datetime.combine(log.visit_date, datetime.min.time())
            recent_activity.append(
                {
                    "kind": "log",
                    "title": "Service log added",
                    "description": log.client.name if log.client else "Service activity",
                    "timestamp": log_timestamp,
                    "icon": "fa-clipboard-check",
                    "tone": "green",
                }
            )
        recent_activity = sorted(
            recent_activity,
            key=lambda item: item["timestamp"] or datetime.min,
            reverse=True,
        )[:8]

        admin_data = {
            "can_view_overall": can_view_overall,
            "pm_week": pm_week,
            "ticket_deadlines": ticket_deadlines,
            "ticket_overview": ticket_overview,
            "active_tickets": active_tickets,
            "logs_week": logs_week,
            "pm_total": pm_total,
            "service_log_stats": service_log_stats,
            "status_counts": status_counts,
            "priority_counts": priority_counts,
            **calendar_data,
            "engineer_performance": engineer_performance,
            "recent_activity": recent_activity,
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
