from threading import Lock

from flask import request
from flask_login import current_user
from flask_socketio import join_room

from . import socketio

_connected_sids = set()
_connected_sids_lock = Lock()


@socketio.on("connect")
def socket_connect():
    if not current_user.is_authenticated:
        return False
    with _connected_sids_lock:
        _connected_sids.add(request.sid)
    return True


@socketio.on("disconnect")
def socket_disconnect():
    with _connected_sids_lock:
        _connected_sids.discard(request.sid)


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


@socketio.on("join_header_notifications")
def join_header_notifications():
    if current_user.is_authenticated:
        join_room(f"user_notifications:{current_user.id}")
