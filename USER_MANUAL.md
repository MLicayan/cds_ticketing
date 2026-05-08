CDS Service Monitoring – User Manual
====================================

Overview
--------
- Web app for tracking tickets, service logs, preventive/weekly schedules, instruments, and reports.
- Uses roles to control access; key data lives under Tickets, Service Logs, Instruments, PM/Weekly schedules, and Reports.

Roles & Permissions
-------------------
- **Admin**: full access; manage users (admins, engineers/IT, sales, clients), clients, instruments, apps, parts, service types; view all data.
- **Engineer/IT**: full access to tickets and service logs; can create/edit logs, schedules, exports, and run reports.
- **Sales**: can create tickets and view modules; cannot change ticket status/priority/assignee or create service logs.
- **Client**: can create tickets for own client; view their tickets and logs; cannot edit ticket status/assignee/priority; must approve closures with a signature.

Navigation
----------
- **Dashboard**: snapshots; scoped to your role/client.
- **Tickets**: list, filters, Gantt, calendar, kanban, CSV export.
- **Service Logs**: list, filters, Gantt, CSV/PDF exports.
- **Weekly Schedule / PM Schedule**: plan work; link PMs to service logs.
- **Instruments**: inventory; per-instrument service history.
- **Reports**: top instruments/engineers (admin/engineer only).
- **Admin**: manage users, clients, instruments, parts, apps, service types (admin only).

Ticket Workflow
---------------
1) **Create**  
   - Admin/Engineer/Sales: Tickets → New Ticket.  
   - Client: Tickets → New Ticket (auto-scoped to own client).  
   - Required: client, subject, ticket target (instrument or app). Optional photo upload.
   - Ticket numbers format: `T-000001` (6 digits).

2) **View/Update**  
   - Ticket detail shows status, priority, assignee, comments, timeline, attachments, linked service logs.  
   - Admin/Engineer can change status, priority, target date, and assignee. Sales/Clients are read-only for these fields.  
   - Comments: clients and sales can add public comments; internal notes are staff-only.

3) **Client signature required on closure (client-reported tickets)**  
   - When changing status to **Closed** for tickets reported by a **Client** user, a signature modal appears.  
   - Capture the client’s signature (draw in the modal) and submit; the signature is saved as a ticket attachment and closure proceeds.

4) **Resolution flow**  
   - Status options: Open, In-Process, Fix/Completed (Resolved), Re-Open, Closed, Cancelled, On Hold.  
   - When a linked service log sets status_after to operational, the ticket moves to Resolved; non-operational keeps it In-Process.

Service Log Workflow
--------------------
- **Access**: Admin/Engineer can create/edit; Sales/Clients cannot.
- **Create**: Service Logs → New. Optionally prefilled when opened from a ticket/PM.  
  - Required: client, instrument or app (with service_for selector), engineer, visit date, confirmation photo, confirmed by/person, onsite signature (auto-required for non-remote types).  
  - Times: capture **Start Time** and **End Time**.  
  - Parts: add rows with qty/price; attachments saved automatically.  
  - On save: creates attachments (photo, signature if provided) and updates linked ticket status (operational → Resolved; otherwise In-Process).
- **View/Edit**: Service log detail shows summary, start/end times, monitoring flag, attachments; admins/assigned engineer can edit and upload attachments.
- **Exports**: CSV export from Service Logs list; PDF per log.

Schedules
---------
- **PM Schedule**: plan preventive maintenance; assign engineer; link to service log creation; filters by client/instrument/date.
- **Weekly Schedule**: plan weekly tasks; can create tickets from schedule entries.

Instruments
-----------
- Inventory with client, model/brand/SN, status, LIS data, install/warranty dates.
- Per-instrument logs view (QR link available) lists service history; open log detail from the table.

Reports
-------
- Admin/Engineer only; shows top instruments (visits/defects) and engineers (log counts). Clients see scoped data only.

Attachments & Signatures
------------------------
- Uploads stored per module (tickets, service logs, PM schedules).  
- Ticket closure for client-reported tickets requires a ticket-level signature (captured on status change).  
- Service logs require onsite signatures for non-remote types; uploaded as attachments.

Exports
-------
- Tickets: CSV from Tickets page.  
- Service Logs: CSV from Logs page; PDF per log.

Admin Setup
-----------
- Create users under Admin: Admins, Engineers/IT, Sales, Clients.  
- Ensure `UPLOAD_FOLDER_*` paths are configured for tickets, service logs, PM schedules.  
- For legacy scrypt password hashes on old Python/OpenSSL, login is supported; new passwords use PBKDF2.

Troubleshooting
---------------
- Cannot close client-reported ticket: ensure a signature is captured in the closure modal.  
- Service log save blocked: check required fields (client, instrument/app, engineer, visit date, photo, signature for onsite types).  
- Missing start/end time: edit the log and save times; PDFs reflect these fields.  
- Permission errors: confirm the role (Sales/Client are read-only for many actions).

Step-by-Step by Module
----------------------
**Tickets**
- Create: Tickets → New Ticket → select client/instrument or app → fill subject/description → (optional) attach photo → Save.
- Filter: use client/instrument/priority/status/date filters; reset with Reset.
- View: click a ticket row → see status, priority, assignee, comments, service logs, attachments.
- Update (Admin/Engineer): change status/priority/target date/assignee; client-reported closure prompts for signature.
- Comment: add text; staff can mark Internal; clients/sales add public comments only.
- Exports/Views: use Gantt, Calendar, Kanban, or Export CSV from Tickets list.

**Service Logs**
- Create (Admin/Engineer): Service Logs → New → select client, service for (instrument/app), engineer, visit date, start/end time, status_after, confirmation photo, confirmed by/position; capture signature for onsite types; add parts if needed → Save.
- Link: open from a ticket or PM schedule to prefill fields.
- Edit: open a log, click Edit Log (admin or assigned engineer) → adjust fields, times, monitoring, add attachments.
- Exports: Service Logs list → Export CSV; log detail → Generate PDF.

**Preventive Maintenance Schedules**
- Create: PM Schedule → New → select client/instrument, date, description, assign engineer → Save.
- View/Filter: filter by client, instrument, date range.
- Action: open schedule detail → create linked service log when work is done.

**Weekly Schedules**
- Create: Weekly Schedule → New → set week start/title → add tasks (client + instrument/app + subject/service type + engineer/priority) → Save.
- Use: open schedule, access tasks; tasks can spawn tickets where applicable.

**Instruments**
- View list: Instruments menu; filter by client (for staff) or auto-scoped (clients).
- Detail/logs: open an instrument → see service history (logs table), QR link, last PM/calibration.
- From logs table: click a row to open the service log detail.

**Reports**
- Access (Admin/Engineer): Reports menu → view top instruments by visits/defects and top engineers by log counts.
- Filters: client scoping applies automatically for clients; staff see global.

**Admin**
- Users: manage Admins, Engineers/IT, Sales, Clients. Set username, full name, password, role, optional client.
- Clients: create/edit clients, map apps, add client users.
- Instruments/Apps/Service Types/Parts: manage reference data; ensure instruments link to clients.
