from datetime import datetime

from flask import Blueprint, request, jsonify

from . import db
from .models import Instrument, LISLog

lis_api_bp = Blueprint("lis_api", __name__)


def _parse_direction(value: str) -> str:
    val = (value or "").lower()
    return val if val in ("rx", "tx") else "rx"


@lis_api_bp.route("/api/lis/logs", methods=["POST"])
def create_lis_log():
    data = request.get_json(silent=True) or {}

    instrument_code = data.get("instrument_code")
    instrument_id = data.get("instrument_id")
    message = data.get("message")
    direction = _parse_direction(data.get("direction"))

    instrument = None
    if instrument_id:
        instrument = Instrument.query.get(instrument_id)
    if not instrument and instrument_code:
        instrument = Instrument.query.filter_by(code=instrument_code).first()

    if not instrument or not message:
        return jsonify({"error": "instrument and message are required"}), 400

    log = LISLog(
        instrument_id=instrument.id,
        direction=direction,
        message=message,
        created_at=datetime.utcnow(),
    )
    db.session.add(log)

    # Optional instrument metadata updates
    status = data.get("lis_status")
    protocol = data.get("lis_protocol")
    last_active = data.get("lis_last_active_at")
    last_sent = data.get("lis_last_sent_at")
    last_received = data.get("lis_last_received_at")

    if status:
        instrument.lis_status = status
    if protocol:
        instrument.lis_protocol = protocol

    def _parse_dt(val):
        if not val:
            return None
        if isinstance(val, (int, float)):
            return datetime.utcfromtimestamp(val)
        try:
            return datetime.strptime(val, "%Y-%m-%dT%H:%M:%S.%fZ")
        except Exception:
            return None

    ts_active = _parse_dt(last_active)
    ts_sent = _parse_dt(last_sent)
    ts_received = _parse_dt(last_received)

    instrument.lis_last_active_at = ts_active or instrument.lis_last_active_at
    instrument.lis_last_sent_at = ts_sent or instrument.lis_last_sent_at
    instrument.lis_last_received_at = ts_received or instrument.lis_last_received_at

    db.session.commit()

    return jsonify({"status": "ok", "log_id": log.id}), 201


@lis_api_bp.route("/api/lis/logs", methods=["GET"])
def list_lis_logs():
    instrument_code = request.args.get("instrument_code")
    instrument_id = request.args.get("instrument_id")
    limit_raw = request.args.get("limit") or "50"
    try:
        limit = min(max(int(limit_raw), 1), 200)
    except ValueError:
        limit = 50

    instrument = None
    if instrument_id:
        instrument = Instrument.query.get(instrument_id)
    if not instrument and instrument_code:
        instrument = Instrument.query.filter_by(code=instrument_code).first()

    if not instrument:
        return jsonify({"error": "instrument not found"}), 404

    logs = (
        LISLog.query.filter_by(instrument_id=instrument.id)
        .order_by(LISLog.created_at.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        [
            {
                "id": log.id,
                "instrument_id": log.instrument_id,
                "direction": log.direction,
                "message": log.message,
                "created_at": (log.created_at.isoformat() if log.created_at else None),
            }
            for log in logs
        ]
    )





# curl -X POST http://localhost:5012/api/lis/logs  -H "Content-Type: application/json"  -d "{\"instrument_code\":\"CDS-P500\",\"direction\":\"rx\",\"message\":\"ENQ\",\"lis_status\":\"connected\",\"lis_protocol\":\"ASTM\",\"lis_last_active_at\":\"2025-02-07 10:35:00\",\"lis_last_sent_at\":\"2025-02-07 10:25:00\",\"lis_last_received_at\":\"2025-02-07 10:35:05\"}"

# curl -X POST http://localhost:5012/api/lis/logs  -H "Content-Type: application/json"  -d "{\"instrument_code\":\"CDS-P500\",\"direction\":\"tx\",\"message\":\"ACK\",\"lis_status\":\"connected\",\"lis_protocol\":\"ASTM\",\"lis_last_active_at\":\"2025-02-07 10:35:00\",\"lis_last_sent_at\":\"2025-02-07 10:25:00\",\"lis_last_received_at\":\"2025-02-07 10:35:05\"}"
