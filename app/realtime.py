from flask_login import current_user
from flask_socketio import join_room

from . import socketio


@socketio.on("join_ticket_list")
def join_ticket_list():
    if current_user.is_authenticated:
        join_room("tickets")


@socketio.on("join_ticket_detail")
def join_ticket_detail(data):
    if not current_user.is_authenticated:
        return
    ticket_id = (data or {}).get("ticket_id")
    if ticket_id:
        join_room(f"ticket:{ticket_id}")
