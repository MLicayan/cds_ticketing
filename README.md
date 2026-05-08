# Instrument Monitoring Flask App (AdminLTE + DataTables)

Features:
- AdminLTE 3 layout
- Tickets module (list, create, detail, comments)
- Service Logs module (filters, link to tickets)
- Instruments list
- DataTables on Tickets + Service Logs with paging & sorting
- CSV export for Tickets and Service Logs

Documentation:
- Ticketing: `docs/TICKETING_DOCUMENTATION.md`

## Quick start

```bash
python -m venv venv
# Windows:
venv\Scripts\activate
# Linux/mac:
source venv/bin/activate

pip install -r requirements.txt

set FLASK_APP=run.py  # Windows
# or:
export FLASK_APP=run.py

flask db init
flask db migrate -m "Initial tables"
flask db upgrade
```

Create an admin user using a Python shell:

```python
from app import create_app, db
from app.models import User, UserRole, Client

app = create_app()
app.app_context().push()

client = Client(name="Demo Lab", client_code="LAB-001")
db.session.add(client)
db.session.commit()

u = User(username="admin", full_name="Admin User", role=UserRole.ADMIN, client_id=client.id)
u.set_password("admin123")
db.session.add(u)
db.session.commit()
```

Run:

```bash
flask run
# or
python run.py
```
