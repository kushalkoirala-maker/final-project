import hashlib
import json

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required, current_user

from ..db import db
from ..models.alert import Alert
from ..models.config_snapshot import ConfigSnapshot
from ..models.device import Device
from ..models.job import Job
from ..services.diff import generate_diff
from ..services.monitor import fetch_monitoring_snapshot
from ..services.job_worker import submit_job
from ..services.ssh_client import run_show_command, push_config_commands, test_connection
from ..services.template_engine import (
    available_templates,
    template_schema_map,
    render_template_commands,
    TemplateNotAvailableError,
    TemplateRenderError,
)
from ..services.validator import validate_commands

api_devices_bp = Blueprint("api_devices", __name__)
SSH_TIMEOUT_SECONDS = 20
MAX_CONFIG_OUTPUT_CHARS = 200_000
MAX_DIFF_OUTPUT_CHARS = 200_000
MAX_TEMPLATE_COMMANDS = 5_000
MAX_ROLLBACK_COMMANDS = 20_000
MAX_JOB_RESULT_CHARS = 50_000


def _truncate_output(text: str, limit: int) -> tuple[str, bool]:
    value = text or ""
    if len(value) <= limit:
        return value, False
    notice = f"\n\n... [truncated to {limit} chars]"
    head = value[: max(0, limit - len(notice))]
    return f"{head}{notice}", True


def _audit(severity: str, message: str, device_id: int | None = None) -> None:
    db.session.add(Alert(severity=severity, message=message[:255], device_id=device_id))
    db.session.commit()


def _config_to_commands(config_text: str) -> list[str]:
    commands: list[str] = []
    for raw in (config_text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("!"):
            continue
        # Skip common non-command noise from show outputs.
        lowered = stripped.lower()
        if lowered.startswith("building configuration"):
            continue
        if lowered.startswith("current configuration"):
            continue
        commands.append(line)
    return commands


def _verify_required_commands(running_config: str, required_commands: list[str]) -> tuple[bool, list[str]]:
    normalized_running = {line.strip() for line in (running_config or "").splitlines() if line.strip()}
    missing = [cmd for cmd in required_commands if cmd.strip() and cmd.strip() not in normalized_running]
    return len(missing) == 0, missing


def _capture_checkpoint(device: Device) -> tuple[bool, ConfigSnapshot | None, str]:
    pre_result = run_show_command(device, "show running-config", timeout=SSH_TIMEOUT_SECONDS)
    if not pre_result.get("success"):
        error, _ = _truncate_output(pre_result.get("error", ""), 4_000)
        return False, None, error

    config_text = pre_result.get("output", "")
    config_hash = hashlib.sha256(config_text.encode("utf-8")).hexdigest()
    snapshot = ConfigSnapshot(device_id=device.id, config_text=config_text, config_hash=config_hash)
    db.session.add(snapshot)
    db.session.commit()
    return True, snapshot, ""


def _create_job(device_id: int, job_type: str, payload: dict, device_ids: list[int] | None = None, user_id: int | None = None) -> Job:
    job = Job(
        user_id=user_id or current_user.id,
        device_id=device_id,
        type=job_type,
        payload_json=json.dumps(payload),
        status="pending",
    )
    if device_ids:
        job.set_device_ids(device_ids)
    else:
        job.set_device_ids([device_id])
    db.session.add(job)
    db.session.commit()
    return job


def _serialize_job(job: Job) -> dict:
    result_preview = (job.result_text or "")
    result_truncated = False
    if len(result_preview) > MAX_JOB_RESULT_CHARS:
        result_preview, result_truncated = _truncate_output(result_preview, MAX_JOB_RESULT_CHARS)

    return {
        "id": job.id,
        "device_id": job.device_id,
        "device_ids": job.get_device_ids(),
        "type": job.type,
        "status": job.status,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "device_results": job.get_device_results(),
        "result_text": result_preview,
        "result_truncated": result_truncated,
    }


def execute_apply_template(
    device_id: int,
    template_name: str,
    variables: dict,
    allow_risky_commands: bool = False,
) -> tuple[bool, dict]:
    device = Device.query.get(device_id)
    if device is None:
        return False, {"error": "device not found"}

    template_name = (template_name or "").strip()
    if not template_name:
        return False, {"error": "template is required", "available_templates": available_templates()}
    if not isinstance(variables, dict):
        return False, {"error": "variables must be an object"}

    try:
        rendered_commands = render_template_commands(template_name, variables)
    except TemplateNotAvailableError as exc:
        return False, {"error": str(exc), "available_templates": available_templates()}
    except TemplateRenderError as exc:
        return False, {"error": f"template render failed: {exc}"}

    if not rendered_commands:
        return False, {"error": "template rendered no commands"}
    if len(rendered_commands) > MAX_TEMPLATE_COMMANDS:
        return False, {"error": f"too many commands rendered (max {MAX_TEMPLATE_COMMANDS})"}

    validation_result = validate_commands(
        rendered_commands,
        variables=variables,
        allow_risky_commands=allow_risky_commands,
    )
    if not validation_result.get("passed"):
        _audit("warn", f"Apply template blocked by validation on {device.name}", device.id)
        return False, {"error": "validation failed", "validation": validation_result}

    pre_ok, snapshot, pre_error = _capture_checkpoint(device)
    if not pre_ok or snapshot is None:
        _audit("crit", f"Apply template failed (pre-check): {device.name} - {pre_error}", device.id)
        return False, {"error": f"failed to capture pre-change config: {pre_error}"}

    pre_config = snapshot.config_text
    _audit("info", f"Checkpoint saved for {device.name} (snapshot #{snapshot.id})", device.id)

    push_result = push_config_commands(device, rendered_commands, timeout=SSH_TIMEOUT_SECONDS)
    if not push_result.get("success"):
        error, _ = _truncate_output(push_result.get("error", ""), 4_000)
        _audit("crit", f"Apply template push failed: {device.name} - {error}", device.id)
        return False, {"snapshot_id": snapshot.id, "error": error}

    # Verification 1: device is reachable after push.
    reachability_check = run_show_command(device, "show running-config", timeout=SSH_TIMEOUT_SECONDS)
    if not reachability_check.get("success"):
        rb_checkpoint_ok, rb_checkpoint, rb_checkpoint_error = _capture_checkpoint(device)
        if rb_checkpoint_ok and rb_checkpoint is not None:
            _audit("info", f"Checkpoint saved before auto-rollback on {device.name} (snapshot #{rb_checkpoint.id})", device.id)
        else:
            _audit("warn", f"Checkpoint before auto-rollback failed on {device.name}: {rb_checkpoint_error}", device.id)

        rollback_commands = _config_to_commands(pre_config)
        if len(rollback_commands) > MAX_ROLLBACK_COMMANDS:
            _audit("crit", f"Rollback failed: snapshot too large for {device.name}", device.id)
            return False, {
                "snapshot_id": snapshot.id,
                "error": "verification failed and rollback aborted (snapshot too large)",
            }

        rollback_result = push_config_commands(device, rollback_commands, timeout=SSH_TIMEOUT_SECONDS)
        if rollback_result.get("success"):
            _audit("warn", f"Auto-rollback succeeded for {device.name} (snapshot #{snapshot.id})", device.id)
        else:
            rollback_error, _ = _truncate_output(rollback_result.get("error", ""), 4_000)
            _audit("crit", f"Auto-rollback failed for {device.name}: {rollback_error}", device.id)

        verify_error, _ = _truncate_output(reachability_check.get("error", ""), 4_000)
        return False, {
            "snapshot_id": snapshot.id,
            "error": f"verification failed (device unreachable): {verify_error}",
            "rollback_success": bool(rollback_result.get("success")),
        }

    # Verification 2: required rendered commands exist in running config.
    post_running_config = reachability_check.get("output", "")
    verified, missing_commands = _verify_required_commands(post_running_config, rendered_commands)
    if not verified:
        rb_checkpoint_ok, rb_checkpoint, rb_checkpoint_error = _capture_checkpoint(device)
        if rb_checkpoint_ok and rb_checkpoint is not None:
            _audit("info", f"Checkpoint saved before auto-rollback on {device.name} (snapshot #{rb_checkpoint.id})", device.id)
        else:
            _audit("warn", f"Checkpoint before auto-rollback failed on {device.name}: {rb_checkpoint_error}", device.id)

        rollback_commands = _config_to_commands(pre_config)
        if len(rollback_commands) > MAX_ROLLBACK_COMMANDS:
            _audit("crit", f"Rollback failed: snapshot too large for {device.name}", device.id)
            return False, {
                "snapshot_id": snapshot.id,
                "error": "verification failed and rollback aborted (snapshot too large)",
                "missing_commands": missing_commands[:50],
            }

        rollback_result = push_config_commands(device, rollback_commands, timeout=SSH_TIMEOUT_SECONDS)
        if rollback_result.get("success"):
            _audit("warn", f"Auto-rollback succeeded for {device.name} (snapshot #{snapshot.id})", device.id)
        else:
            rollback_error, _ = _truncate_output(rollback_result.get("error", ""), 4_000)
            _audit("crit", f"Auto-rollback failed for {device.name}: {rollback_error}", device.id)

        _audit("crit", f"Verification failed after apply for {device.name}; rollback attempted", device.id)
        return False, {
            "snapshot_id": snapshot.id,
            "error": "verification failed: rendered commands not found in running-config",
            "missing_commands": missing_commands[:50],
            "rollback_success": bool(rollback_result.get("success")),
        }

    _audit("info", f"Template applied successfully on {device.name} (snapshot #{snapshot.id})", device.id)
    push_output, push_output_truncated = _truncate_output(push_result.get("output", ""), 20_000)
    return True, {
        "snapshot_id": snapshot.id,
        "template": template_name,
        "applied_commands": rendered_commands,
        "push_output": push_output,
        "push_output_truncated": push_output_truncated,
    }


@api_devices_bp.get("/devices")
@login_required
def list_devices():
    devices = Device.query.all()
    return jsonify([
        {
            "id": d.id,
            "name": d.name,
            "ip_address": d.ip_address,
            "device_type": d.device_type,
            "vendor": d.vendor,
            "location": d.location,
            "is_up": d.is_up,
        } for d in devices
    ])


@api_devices_bp.get("/monitor")
@login_required
def monitor_inventory():
    devices = Device.query.order_by(Device.name.asc()).all()
    payload = fetch_monitoring_snapshot(devices=devices, update_inventory=True)
    return jsonify(payload)


@api_devices_bp.post("/devices")
@login_required
def add_device():
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    ip = (data.get("ip_address") or "").strip()
    device_type = data.get("device_type", "router")
    vendor = data.get("vendor", "cisco")
    location = (data.get("location") or "").strip() or None

    if not name or not ip:
        return jsonify({"error": "name and ip_address required"}), 400

    if Device.query.filter_by(ip_address=ip).first():
        return jsonify({"error": "ip already exists"}), 400

    d = Device(name=name, ip_address=ip, device_type=device_type, vendor=vendor, location=location)
    db.session.add(d)
    db.session.commit()
    return jsonify({"status": "ok", "id": d.id})


@api_devices_bp.delete("/devices/<int:device_id>")
@login_required
def delete_device(device_id: int):
    """Delete a device and its related rows (alerts/snapshots/jobs)."""
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    Alert.query.filter_by(device_id=device.id).delete(synchronize_session=False)
    ConfigSnapshot.query.filter_by(device_id=device.id).delete(synchronize_session=False)
    Job.query.filter_by(device_id=device.id).delete(synchronize_session=False)
    db.session.delete(device)
    db.session.commit()
    return jsonify({"success": True})


@api_devices_bp.post("/alerts/clear")
@login_required
def api_clear_alerts():
    """Clear alerts (admin-only)."""
    if current_user.role != "admin":
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    scope = (data.get("scope") or "filtered").strip().lower()
    severity = (data.get("severity") or "").strip().lower()
    device_id = data.get("device_id")

    query = Alert.query
    if scope != "all":
        if severity in ("info", "warn", "crit"):
            query = query.filter_by(severity=severity)
        if isinstance(device_id, int):
            query = query.filter_by(device_id=device_id)

    deleted = query.delete(synchronize_session=False)
    db.session.commit()
    return jsonify({"success": True, "deleted": deleted})


@api_devices_bp.post("/devices/<int:device_id>/run")
@login_required
def run_device_command(device_id: int):
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    command = (data.get("command") or "").strip()
    if not command:
        return jsonify({"error": "command is required"}), 400

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    result = run_show_command(device, command)
    status_code = 200 if result.get("success") else 502
    return jsonify(result), status_code


@api_devices_bp.post("/devices/<int:device_id>/test-connection")
@login_required
def test_device_connection(device_id: int):
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    result = test_connection(device)
    status_code = 200 if result.get("success") else 502
    return jsonify(result), status_code


@api_devices_bp.get("/devices/<int:device_id>/config")
@login_required
def get_device_running_config(device_id: int):
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    result = run_show_command(device, "show running-config", timeout=SSH_TIMEOUT_SECONDS)
    output, output_truncated = _truncate_output(result.get("output", ""), MAX_CONFIG_OUTPUT_CHARS)
    error, error_truncated = _truncate_output(result.get("error", ""), 4_000)

    response = {
        "success": bool(result.get("success")),
        "output": output,
        "error": error,
        "output_truncated": output_truncated,
        "error_truncated": error_truncated,
    }
    return jsonify(response), (200 if response["success"] else 502)


@api_devices_bp.post("/templates/preview")
@login_required
def preview_config_template():
    data = request.get_json(silent=True) or {}
    template_name = (data.get("template") or "").strip()
    variables = data.get("variables") or {}
    allow_risky_commands = bool(data.get("allow_risky_commands", False))

    if not template_name:
        return jsonify({"error": "template is required", "available_templates": available_templates()}), 400
    if not isinstance(variables, dict):
        return jsonify({"error": "variables must be an object"}), 400

    try:
        commands = render_template_commands(template_name, variables)
    except TemplateNotAvailableError as exc:
        return jsonify({"error": str(exc), "available_templates": available_templates()}), 400
    except TemplateRenderError as exc:
        return jsonify({"error": f"template render failed: {exc}"}), 400

    validation_result = validate_commands(
        commands,
        variables=variables,
        allow_risky_commands=allow_risky_commands,
    )
    if not validation_result.get("passed"):
        return jsonify({"error": "validation failed", "validation": validation_result}), 400

    return jsonify(
        {
            "success": True,
            "template": template_name,
            "commands": commands,
        }
    )


@api_devices_bp.get("/templates/schema")
@login_required
def templates_schema():
    return jsonify(
        {
            "success": True,
            "templates": available_templates(),
            "schemas": template_schema_map(),
        }
    )


@api_devices_bp.post("/devices/<int:device_id>/diff")
@login_required
def diff_device_config(device_id: int):
    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    data = request.get_json(silent=True) or {}
    template_name = (data.get("template") or "").strip()
    variables = data.get("variables") or {}
    allow_risky_commands = bool(data.get("allow_risky_commands", False))

    if not template_name:
        return jsonify({"error": "template is required", "available_templates": available_templates()}), 400
    if not isinstance(variables, dict):
        return jsonify({"error": "variables must be an object"}), 400

    try:
        rendered_commands = render_template_commands(template_name, variables)
    except TemplateNotAvailableError as exc:
        return jsonify({"error": str(exc), "available_templates": available_templates()}), 400
    except TemplateRenderError as exc:
        return jsonify({"error": f"template render failed: {exc}"}), 400

    validation_result = validate_commands(
        rendered_commands,
        variables=variables,
        allow_risky_commands=allow_risky_commands,
    )
    if not validation_result.get("passed"):
        return jsonify({"error": "validation failed", "validation": validation_result}), 400

    if len(rendered_commands) > MAX_TEMPLATE_COMMANDS:
        return jsonify({"error": f"too many commands rendered (max {MAX_TEMPLATE_COMMANDS})"}), 400

    current_result = run_show_command(device, "show running-config", timeout=SSH_TIMEOUT_SECONDS)
    if not current_result.get("success"):
        output, output_truncated = _truncate_output(current_result.get("output", ""), MAX_CONFIG_OUTPUT_CHARS)
        error, error_truncated = _truncate_output(current_result.get("error", ""), 4_000)
        return jsonify(
            {
                "success": False,
                "error": error,
                "output": output,
                "output_truncated": output_truncated,
                "error_truncated": error_truncated,
            }
        ), 502

    running_config = current_result.get("output", "")
    rendered_config_text = "\n".join(rendered_commands)
    if rendered_config_text:
        rendered_config_text += "\n"

    diff_text = generate_diff(running_config, rendered_config_text)
    diff_text, diff_truncated = _truncate_output(diff_text, MAX_DIFF_OUTPUT_CHARS)

    return jsonify(
        {
            "success": True,
            "template": template_name,
            "rendered_commands": rendered_commands,
            "diff": diff_text,
            "diff_truncated": diff_truncated,
        }
    )


@api_devices_bp.post("/devices/<int:device_id>/apply_template")
@login_required
def apply_template_to_device(device_id: int):
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    data = request.get_json(silent=True) or {}
    template_name = (data.get("template") or "").strip()
    variables = data.get("variables") or {}
    allow_risky_commands = bool(data.get("allow_risky_commands", False))

    if not template_name:
        return jsonify({"error": "template is required", "available_templates": available_templates()}), 400
    if not isinstance(variables, dict):
        return jsonify({"error": "variables must be an object"}), 400

    payload = {
        "template": template_name,
        "variables": variables,
        "allow_risky_commands": allow_risky_commands,
    }
    job = _create_job(device.id, "apply_template", payload, device_ids=[device.id])
    submit_job(job.id, current_app._get_current_object())
    _audit("info", f"Apply-template job queued for {device.name} (job #{job.id})", device.id)

    return jsonify({"success": True, "job": _serialize_job(job)}), 202


@api_devices_bp.post("/devices/<int:device_id>/snapshot")
@login_required
def create_snapshot_job_api(device_id: int):
    """
    Triggers a background job to capture a full configuration snapshot via SSH.
    Enterprise Logic: Uses background workers to prevent UI timeout during long SSH sessions.
    """
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    # Create the job record with capture_snapshot type
    payload = {"command": "show running-config"}
    job = _create_job(device.id, "capture_snapshot", payload, device_ids=[device.id])
    
    # Submit to the ThreadPoolExecutor
    submit_job(job.id, current_app._get_current_object())
    _audit("info", f"Snapshot capture job queued for {device.name} (job #{job.id})", device.id)

    return jsonify({
        "success": True, 
        "message": f"Snapshot job for {device.name} started.", 
        "job_id": job.id,
        "job": _serialize_job(job)
    }), 202


@api_devices_bp.post("/jobs")
@login_required
def create_job():
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    data = request.get_json(silent=True) or {}
    device_id = data.get("device_id")
    raw_device_ids = data.get("device_ids")
    job_type = (data.get("type") or "").strip()
    payload = data.get("payload") or {}

    device_ids: list[int] = []
    if isinstance(raw_device_ids, list):
        for value in raw_device_ids:
            try:
                device_ids.append(int(value))
            except Exception:
                return jsonify({"error": "device_ids must contain integers"}), 400
    elif isinstance(device_id, int):
        device_ids = [device_id]
    else:
        return jsonify({"error": "device_id or device_ids is required"}), 400

    device_ids = sorted(set(device_ids))
    if not device_ids:
        return jsonify({"error": "at least one device is required"}), 400
    if not job_type:
        return jsonify({"error": "type is required"}), 400
    if not isinstance(payload, dict):
        return jsonify({"error": "payload must be an object"}), 400

    devices = Device.query.filter(Device.id.in_(device_ids)).all()
    found_ids = {d.id for d in devices}
    missing = [d for d in device_ids if d not in found_ids]
    if missing:
        return jsonify({"error": "one or more devices not found", "missing_device_ids": missing}), 404

    if job_type != "apply_template":
        return jsonify({"error": "unsupported job type", "supported_types": ["apply_template"]}), 400

    if not (payload.get("template") or "").strip():
        return jsonify({"error": "payload.template is required for apply_template jobs"}), 400

    job = _create_job(device_ids[0], job_type, payload, device_ids=device_ids)
    submit_job(job.id, current_app._get_current_object())
    _audit("info", f"Job queued for {len(device_ids)} device(s) (job #{job.id}, type={job.type})", device_ids[0])
    return jsonify({"success": True, "job": _serialize_job(job)}), 201


@api_devices_bp.get("/jobs")
@login_required
def list_jobs():
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    status = (request.args.get("status") or "").strip().lower()
    device_id = request.args.get("device_id", type=int)
    limit = min(max(int(request.args.get("limit", 50)), 1), 200)

    query = Job.query
    if status in ("pending", "running", "success", "failed"):
        query = query.filter_by(status=status)
    jobs = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(limit * 5).all()
    if device_id is not None:
        jobs = [job for job in jobs if job.has_device(device_id)]
    jobs = jobs[:limit]
    return jsonify([_serialize_job(job) for job in jobs])


@api_devices_bp.get("/jobs/<int:job_id>")
@login_required
def get_job_status(job_id: int):
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    job = Job.query.get(job_id)
    if job is None:
        return jsonify({"error": "job not found"}), 404
    return jsonify({"success": True, "job": _serialize_job(job)})


@api_devices_bp.get("/devices/<int:device_id>/snapshots")
@login_required
def device_snapshot_list(device_id: int):
    if current_user.role not in ("admin", "operator", "viewer"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    limit = min(max(int(request.args.get("limit", 50)), 1), 500)
    snapshots = (
        ConfigSnapshot.query.filter_by(device_id=device.id)
        .order_by(ConfigSnapshot.created_at.desc(), ConfigSnapshot.id.desc())
        .limit(limit)
        .all()
    )
    return jsonify(
        [
            {
                "id": s.id,
                "device_id": s.device_id,
                "config_hash": s.config_hash,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in snapshots
        ]
    )


@api_devices_bp.get("/snapshots/<int:snapshot_id>")
@login_required
def snapshot_detail_api(snapshot_id: int):
    if current_user.role not in ("admin", "operator", "viewer"):
        return jsonify({"error": "forbidden"}), 403

    snapshot = ConfigSnapshot.query.get(snapshot_id)
    if snapshot is None:
        return jsonify({"error": "snapshot not found"}), 404

    text, truncated = _truncate_output(snapshot.config_text or "", MAX_CONFIG_OUTPUT_CHARS)
    return jsonify(
        {
            "id": snapshot.id,
            "device_id": snapshot.device_id,
            "config_hash": snapshot.config_hash,
            "created_at": snapshot.created_at.isoformat() if snapshot.created_at else None,
            "config_text": text,
            "config_text_truncated": truncated,
        }
    )


@api_devices_bp.get("/snapshots/<int:snapshot_id>/compare")
@login_required
def compare_snapshot_to_previous_api(snapshot_id: int):
    if current_user.role not in ("admin", "operator", "viewer"):
        return jsonify({"error": "forbidden"}), 403

    snapshot = ConfigSnapshot.query.get(snapshot_id)
    if snapshot is None:
        return jsonify({"error": "snapshot not found"}), 404

    previous_snapshot = (
        ConfigSnapshot.query.filter(
            ConfigSnapshot.device_id == snapshot.device_id,
            ConfigSnapshot.id < snapshot.id,
        )
        .order_by(ConfigSnapshot.id.desc())
        .first()
    )
    if previous_snapshot is None:
        return jsonify({"error": "no previous snapshot available for comparison"}), 404

    diff_text = generate_diff(previous_snapshot.config_text or "", snapshot.config_text or "")
    diff_text, diff_truncated = _truncate_output(diff_text, MAX_DIFF_OUTPUT_CHARS)
    return jsonify(
        {
            "success": True,
            "snapshot_id": snapshot.id,
            "previous_snapshot_id": previous_snapshot.id,
            "diff": diff_text,
            "diff_truncated": diff_truncated,
        }
    )


@api_devices_bp.get("/alerts")
@login_required
def list_alerts():
    severity = (request.args.get("severity") or "").strip().lower()
    device_id = request.args.get("device_id", type=int)
    limit = min(max(int(request.args.get("limit", 200)), 1), 500)

    query = Alert.query
    if severity in ("info", "warn", "crit"):
        query = query.filter_by(severity=severity)
    if device_id is not None:
        query = query.filter_by(device_id=device_id)

    alerts = query.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit).all()
    return jsonify(
        [
            {
                "id": a.id,
                "severity": a.severity,
                "message": a.message,
                "device_id": a.device_id,
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a in alerts
        ]
    )


@api_devices_bp.post("/devices/<int:device_id>/rollback")
@login_required
def rollback_device_to_latest_snapshot(device_id: int):
    if current_user.role not in ("admin", "operator"):
        return jsonify({"error": "forbidden"}), 403

    device = Device.query.get(device_id)
    if device is None:
        return jsonify({"error": "device not found"}), 404

    snapshot = (
        ConfigSnapshot.query.filter_by(device_id=device.id)
        .order_by(ConfigSnapshot.created_at.desc(), ConfigSnapshot.id.desc())
        .first()
    )
    if snapshot is None:
        return jsonify({"error": "no snapshot found for device"}), 404

    pre_rollback_ok, pre_rollback_snapshot, pre_rollback_error = _capture_checkpoint(device)
    if pre_rollback_ok and pre_rollback_snapshot is not None:
        _audit(
            "info",
            f"Checkpoint saved before manual rollback on {device.name} (snapshot #{pre_rollback_snapshot.id})",
            device.id,
        )
    else:
        _audit("warn", f"Checkpoint before rollback failed on {device.name}: {pre_rollback_error}", device.id)

    rollback_commands = _config_to_commands(snapshot.config_text)
    if not rollback_commands:
        _audit("crit", f"Rollback failed for {device.name}: snapshot empty", device.id)
        return jsonify({"success": False, "error": "snapshot has no rollback commands"}), 400
    if len(rollback_commands) > MAX_ROLLBACK_COMMANDS:
        _audit("crit", f"Rollback failed for {device.name}: snapshot too large", device.id)
        return jsonify({"success": False, "error": f"snapshot too large (max {MAX_ROLLBACK_COMMANDS} commands)"}), 400

    result = push_config_commands(device, rollback_commands, timeout=SSH_TIMEOUT_SECONDS)
    if not result.get("success"):
        error, _ = _truncate_output(result.get("error", ""), 4_000)
        _audit("crit", f"Rollback failed for {device.name}: {error}", device.id)
        return jsonify({"success": False, "snapshot_id": snapshot.id, "error": error}), 502

    post_check = run_show_command(device, "show running-config", timeout=SSH_TIMEOUT_SECONDS)
    if not post_check.get("success"):
        error, _ = _truncate_output(post_check.get("error", ""), 4_000)
        _audit("crit", f"Rollback verification failed for {device.name}: {error}", device.id)
        return jsonify(
            {
                "success": False,
                "snapshot_id": snapshot.id,
                "error": f"rollback pushed but verification failed: {error}",
            }
        ), 502

    new_hash = hashlib.sha256((post_check.get("output", "")).encode("utf-8")).hexdigest()
    hash_match = new_hash == snapshot.config_hash

    _audit("warn", f"Manual rollback completed for {device.name} (snapshot #{snapshot.id})", device.id)
    output, output_truncated = _truncate_output(result.get("output", ""), 20_000)
    return jsonify(
        {
            "success": True,
            "snapshot_id": snapshot.id,
            "rollback_output": output,
            "rollback_output_truncated": output_truncated,
            "config_hash_match": hash_match,
        }
    )
