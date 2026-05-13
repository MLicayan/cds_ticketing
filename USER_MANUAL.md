CDS Service Monitoring - User Manual
====================================

Overview
--------
- CDS Service Monitoring is used to track service tickets, developer/IT tasks, service logs, preventive maintenance schedules, weekly schedules, instruments, application monitoring, and reports.
- Access is role-based. The menu can also be customized per user by an administrator through System Setup/user permissions.
- Most operational work starts from Tickets, My Task, Service Logs, Weekly Schedule, PM Schedule, Instruments, or Monitoring.

Roles & Permissions
-------------------
- **Admin**: full access to operational modules, reports, monitoring, and Administrator setup screens.
- **Engineer / Support**: handles assigned tickets and service logs; support users may see broader dashboard/report data depending on setup.
- **IT**: usually works from My Task or Developer Tasks, plus tickets/service logs when granted.
- **Sales**: creates and follows tickets for assigned clients; cannot change protected ticket fields or create service logs.
- **Client**: creates and follows own tickets, views own instruments, and approves closures where required.
- **Client Admin**: views tickets for the assigned client organization; access is client-scoped.

Main Navigation
---------------
- **Dashboard**: role-scoped KPIs, ticket status summaries, schedules, workload snapshots, and calendar-style summaries.
- **All Tickets / Tickets**: main ticket list with filters, details, comments, attachments, Gantt, calendar, kanban, and CSV export.
- **My Task / My Tickets**: assigned work for staff or client-specific ticket access, depending on role and permissions.
- **Developer Tasks**: IT/developer task board and filterable task list when access is granted.
- **App Monitoring**: current application/integration status view, opened in a separate tab.
- **Daily Monitoring**: daily monitoring view, opened in a separate tab.
- **Service Logs**: onsite/remote service records, parts used, signatures, exports, and PDF generation.
- **Weekly Schedule**: weekly planning with task rows that can be connected to tickets or PM work.
- **PM Schedule**: preventive maintenance planning with comments, attachments, assignee, duration, and linked service log creation.
- **Instruments**: client instrument inventory, service history, QR links, and LIS status information.
- **Reports**: visit/defect and engineer activity summaries, scoped by access.
- **Profile**: update personal account details and password; client admins may manage users under their client when enabled.
- **Administrator**: system setup, users, clients, instruments, instrument models, parts, CDS applications, and service types.

Ticket Workflow
---------------
1. **Create a ticket**
   - Go to Tickets -> New Ticket.
   - Select the client and whether the ticket is for an instrument or CDS application.
   - Fill in subject, description, priority/date details where available, and optional attachment/photo.
   - Client users are automatically scoped to their own client.
   - Ticket numbers use the `T-000001` format.

2. **Review and update**
   - Open a ticket row to view status, priority, assignee, target/date-needed fields, timeline, comments, attachments, related tasks, and linked service logs.
   - Admin/Engineer users can update status, priority, target date, and assignee when permitted.
   - Sales, Client, and Client Admin users have read-only access to protected fields.

3. **Use comments**
   - Add public comments for normal collaboration.
   - Staff can mark comments as internal when the note should be hidden from client-facing users.
   - Replies keep the conversation threaded.
   - Reactions/acknowledgements can be used to show that a comment was seen or accepted.
   - New client comments can appear in the header notification bell for staff.

4. **Track progress**
   - Status values include Open, In-Process, On Hold, Fix/Completed, Re-Open, Closed, and Cancelled.
   - Kanban supports drag-and-drop status movement for permitted staff on non-closed tickets.
   - Gantt and Calendar views help review timing and workload.

5. **Close client-reported tickets**
   - Closing a ticket reported by a client requires a captured client signature.
   - The closure modal saves the signature as a ticket attachment before the ticket is closed.
   - Closing a parent ticket also closes its child tasks.

Developer / IT Tasks
--------------------
- Developer Tasks are child work items connected to a parent ticket.
- Tasks have their own task number, assignee, status, priority, comments, replies, reactions, attachments, and working/not-working state.
- Use the Developer Tasks page to search and filter by client, assigned IT user, priority, status, and date range.
- Open a task to update permitted fields and collaborate without changing the parent ticket directly.
- Closed tasks are read-only.

Service Log Workflow
--------------------
- **Access**: Admin and permitted Engineer/IT users can create and edit service logs. Sales and client-facing users cannot create service logs.
- **Create**: Go to Service Logs -> New, or launch from a ticket/PM schedule to prefill fields.
- **Required details** usually include client, instrument or application, engineer, service type, visit date, start/end time, problem/action details, status after service, confirmation photo, confirmed by, and confirmed by position.
- **Signatures**: onsite/non-remote service types require a customer signature; remote service types may skip the signature depending on configuration.
- **Parts**: add parts with quantity, price, total, and warranty flag as needed.
- **Monitoring**: mark logs for monitoring and set monitored days where applicable.
- **Ticket sync**: when a linked log marks the target operational, the ticket moves to Fix/Completed/Resolved; otherwise it remains or returns to In-Process if not closed.
- **Exports**: use CSV from the Service Logs list and PDF from the log detail page.

Preventive Maintenance Schedule
-------------------------------
- Go to PM Schedule -> New to create preventive maintenance work.
- Enter client, instrument, date, description, task duration, and assigned engineer.
- PM numbers are generated automatically.
- Use filters by client, instrument, and date range to find planned work.
- Open a PM detail page to add comments, upload attachments, assign/reassign work, edit the schedule, or create a linked service log when the PM is completed.

Weekly Schedule
---------------
- Go to Weekly Schedule -> New to create a weekly plan.
- Set the week start and title, then add task rows for client, instrument/application, service type, subject, notes, engineer, priority, and optional PM schedule.
- Weekly task rows help drive planned work and can link back to tickets/PM schedules where applicable.

Instruments
-----------
- Instruments are scoped by client access.
- The instrument list shows inventory details such as client, model, serial number, installation/warranty dates, status, and LIS connection information.
- Instrument detail pages show service history, linked tickets/logs, last maintenance/calibration context, and QR access.
- QR links provide direct access to the instrument service history page.

Application & Daily Monitoring
------------------------------
- App Monitoring summarizes the current status of CDS applications/integrations.
- Daily Monitoring provides a day-by-day monitoring view.
- Monitoring pages are available only when the user has the matching navigation permission.

Reports
-------
- Reports are available to Admin and permitted Engineer users.
- Report data includes top instruments by visits/defects and engineer activity/log counts.
- Client-scoped users only see data allowed by their client assignment.

Profile
-------
- Use Profile to update account details and password.
- Client Admin users may manage users under their assigned client when the feature is enabled.

Administrator Setup
-------------------
- **System Setup**: manage navigation permissions available to users.
- **Users**: manage Admins, Engineers/IT, Sales, and client users.
- **Clients**: maintain client records, contact details, assigned sales, status, mapped CDS applications, and client users.
- **Instruments**: maintain instruments and link them to clients and instrument models.
- **Instrument Models**: maintain code, name, brand, and machine type.
- **Parts**: maintain part name, description, unit cost, and price.
- **CDS Application**: maintain application code, name, and description.
- **Service Types**: maintain service type code, name, and description.

Attachments & Signatures
------------------------
- Tickets, ticket tasks, service logs, and PM schedules support attachments.
- Ticket closure signatures are stored as ticket attachments.
- Service log customer signatures are stored as service log attachments.
- Confirmation photos are required for service log creation.

Exports
-------
- Tickets: CSV export from the Tickets page.
- Service Logs: CSV export from the Service Logs page.
- Service Logs: PDF export from a service log detail page.

Troubleshooting
---------------
- **Cannot see a module**: ask an admin to check role and navigation permissions in System Setup/user management.
- **Cannot close a client-reported ticket**: capture and submit the closure signature in the modal.
- **Cannot edit a ticket/task**: closed items and protected fields may be read-only for your role.
- **Service log save is blocked**: check client, target instrument/application, engineer, service type, visit date, confirmation photo, confirmed by details, and required signature.
- **Missing service time in PDF**: edit the service log and fill Start Time and End Time.
- **Client sees too little or too much data**: verify the user's client assignment and whether the account is Client or Client Admin.
