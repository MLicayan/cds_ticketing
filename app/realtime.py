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
        from .tickets import (
            _pending_client_resolution_prompt_state_for_user,
            _emit_workday_prompt_state_for_user,
            _pending_developer_prompt_state_for_user,
        )

        resolution_prompt_payload = _pending_client_resolution_prompt_state_for_user(current_user)
        if resolution_prompt_payload.get("count"):
            resolution_prompt_payload["source"] = "login_snapshot"
            socketio.emit(
                "ticket_resolution_prompt",
                resolution_prompt_payload,
                room=f"user_notifications:{current_user.id}",
            )

        socketio.emit(
            "developer_prompt_snapshot",
            _pending_developer_prompt_state_for_user(current_user),
            room=f"user_notifications:{current_user.id}",
        )
        _emit_workday_prompt_state_for_user(current_user)
