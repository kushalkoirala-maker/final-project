import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from ..db import db
from ..models.device import Device
from ..models.job import Job
from .automation_service import execute_config_job, serialize_device
from .ssh_client import run_show_command

scheduler = BackgroundScheduler()
job_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="job-exec")
MAX_JOB_RESULT_CHARS = 50_000


def _now_utc() -> datetime:
    return datetime.utcnow()


def _truncate_result(text: str) -> str:
    value = text or ""
    if len(value) <= MAX_JOB_RESULT_CHARS:
        return value
    suffix = f"\n\n... [truncated to {MAX_JOB_RESULT_CHARS} chars]"
    return value[: max(0, MAX_JOB_RESULT_CHARS - len(suffix))] + suffix


def _device_ids_for_job(job: Job) -> list[int]:
    ids = job.get_device_ids()
    if ids:
        return ids
    return [job.device_id] if job.device_id is not None else []


def _serialize_job(job: Job) -> dict[str, Any]:
    return {
        "id": job.id,
        "type": job.type,
        "payload_json": job.payload_json,
        "user_id": job.user_id,
        "device_id": job.device_id,
        "device_ids": _device_ids_for_job(job),
    }


def _claim_job(job_id: int) -> dict[str, Any] | None:
    started_at = _now_utc()
    updated = (
        Job.query.filter_by(id=job_id, status="pending")
        .update({"status": "running", "started_at": started_at}, synchronize_session=False)
    )
    if updated != 1:
        db.session.rollback()
        current_app.logger.warning(
            f"[JOB] Failed to claim job {job_id} - already running or not found"
        )
        return None

    db.session.commit()
    job = Job.query.get(job_id)
    if job is None:
        return None

    job.set_device_results([])
    db.session.commit()

    job_data = _serialize_job(job)
    current_app.logger.info(
        f"[JOB] Job {job_data['id']} STARTED - type={job_data['type']}, "
        f"devices={len(job_data['device_ids'])}, user_id={job_data['user_id']}"
    )
    return job_data


def _set_job_device_results(job_id: int, device_results: list[dict]) -> None:
    job = Job.query.get(job_id)
    if job is None:
        current_app.logger.warning(f"[JOB] Cannot save device results; job {job_id} not found")
        return
    job.set_device_results(device_results)
    db.session.commit()


def _mark_job_finished(job_id: int, success: bool, result: dict) -> None:
    """
    Mark job as finished using a freshly loaded ORM object in the current thread.
    """
    job = Job.query.get(job_id)
    if job is None:
        current_app.logger.warning(f"[JOB] Cannot finish job {job_id}; row not found")
        return

    device_ids = _device_ids_for_job(job)
    device_id = job.device_id
    job_type = job.type
    user_id = job.user_id

    job.status = "success" if success else "failed"
    job.finished_at = _now_utc()
    job.result_text = _truncate_result(json.dumps(result, default=str))

    device_str = f"device_ids={device_ids}" if len(device_ids) > 1 else f"device_id={device_id}"

    if not success:
        error_msg = str(result.get("error", "Unknown error"))
        error_lower = error_msg.lower()

        if any(token in error_lower for token in ("auth", "password", "credential", "permission denied")):
            job.error_summary = "Auth"
            category = "Auth"
        elif any(token in error_lower for token in ("timeout", "connection timeout", "connection refused")):
            job.error_summary = "Connection"
            category = "Connection"
        elif any(token in error_lower for token in ("syntax", "invalid command", "% invalid")):
            job.error_summary = "Syntax"
            category = "Syntax"
        elif "validation" in error_lower:
            job.error_summary = "Validation Failed"
            category = "Validation"
        elif "template" in error_lower:
            job.error_summary = "Template Error"
            category = "Template"
        elif "unreachable" in error_lower or "unable to reach" in error_lower:
            job.error_summary = "Device Unreachable"
            category = "Unreachable"
        else:
            job.error_summary = (error_msg[:100] + "...") if len(error_msg) > 100 else error_msg
            category = "Other"

        current_app.logger.warning(
            f"[JOB] Job {job_id} FAILED ({category}) - {device_str} - user_id={user_id}"
        )
    else:
        current_app.logger.info(
            f"[JOB] Job {job_id} SUCCEEDED - type={job_type}, {device_str} - user_id={user_id}"
        )

    db.session.commit()


def _capture_checkpoint_task(app, device_id: int) -> dict:
    from ..api.devices import _audit, _capture_checkpoint

    with app.app_context():
        try:
            device = Device.query.get(device_id)
            if device is None:
                return {"device_id": device_id, "ready": False, "error": "device not found"}

            device_payload = serialize_device(device)
            device_name = device_payload.get("name") or f"Device #{device_id}"
            pre_ok, snapshot, pre_error = _capture_checkpoint(device)
            if not pre_ok or snapshot is None:
                _audit("crit", f"Apply template failed (pre-check): {device_name} - {pre_error}", device_id)
                return {
                    "device_id": device_id,
                    "ready": False,
                    "error": pre_error,
                    "device_name": device_name,
                }

            snapshot_id = snapshot.id
            pre_config = snapshot.config_text
            _audit("info", f"Checkpoint saved for {device_name} (snapshot #{snapshot_id})", device_id)
            return {
                "device_id": device_id,
                "ready": True,
                "snapshot_id": snapshot_id,
                "pre_config": pre_config,
                "device_name": device_name,
                "device_payload": device_payload,
            }
        finally:
            db.session.remove()


def _execute_apply_template_parallel(app, job_data: dict[str, Any], payload: dict) -> None:
    from ..api.devices import _audit, _truncate_output, _verify_required_commands
    from ..services.template_engine import (
        TemplateNotAvailableError,
        TemplateRenderError,
        render_template_commands,
    )
    from ..services.validator import validate_commands

    job_id = int(job_data["id"])
    device_ids = [int(device_id) for device_id in job_data.get("device_ids", [])]
    template_name = (payload.get("template") or "").strip()
    variables = payload.get("variables") or {}
    allow_risky_commands = bool(payload.get("allow_risky_commands", False))
    timeout = int(app.config.get("AUTOMATION_TIMEOUT_SECONDS", 20))

    if not template_name:
        current_app.logger.error(f"[JOB] Job {job_id} - Template name is required")
        _mark_job_finished(job_id, False, {"error": "payload.template is required"})
        return

    try:
        current_app.logger.info(f"[JOB] Job {job_id} - Rendering template: {template_name}")
        rendered_commands = render_template_commands(template_name, variables)
        current_app.logger.info(
            f"[JOB] Job {job_id} - Template rendered successfully ({len(rendered_commands)} commands)"
        )
    except TemplateNotAvailableError as exc:
        current_app.logger.error(f"[JOB] Job {job_id} - Template not available: {exc}")
        _mark_job_finished(job_id, False, {"error": str(exc)})
        return
    except TemplateRenderError as exc:
        current_app.logger.error(f"[JOB] Job {job_id} - Template render failed: {exc}")
        _mark_job_finished(job_id, False, {"error": f"template render failed: {exc}"})
        return

    current_app.logger.info(f"[JOB] Job {job_id} - Validating rendered commands")
    validation_result = validate_commands(
        rendered_commands,
        variables=variables,
        allow_risky_commands=allow_risky_commands,
    )
    if not validation_result.get("passed"):
        current_app.logger.error(
            f"[JOB] Job {job_id} - Command validation failed: {validation_result.get('reasons', [])}"
        )
        _mark_job_finished(job_id, False, {"error": "validation failed", "validation": validation_result})
        return

    device_payloads = [
        serialize_device(device)
        for device in Device.query.filter(Device.id.in_(device_ids)).all()
    ]
    devices_by_id = {int(device["id"]): device for device in device_payloads if device.get("id") is not None}

    checkpoints: dict[int, dict] = {
        device_id: {"ready": False, "error": "device not found"} for device_id in device_ids
    }
    configured_workers = int(app.config.get("AUTOMATION_MAX_WORKERS", 10))
    safe_workers = int(app.config.get("AUTOMATION_SAFE_MAX_WORKERS", 8))
    checkpoint_workers = max(1, min(configured_workers, safe_workers, len(device_ids) or 1))

    current_app.logger.info(
        f"[JOB] Job {job_id} - Starting parallel execution on {len(device_ids)} devices"
    )

    with ThreadPoolExecutor(max_workers=checkpoint_workers, thread_name_prefix="job-checkpoint") as executor:
        future_map = {
            executor.submit(_capture_checkpoint_task, app, device_id): device_id
            for device_id in device_ids
        }
        for future in as_completed(future_map):
            device_id = future_map[future]
            try:
                checkpoints[device_id] = future.result()
            except Exception as exc:
                checkpoints[device_id] = {"ready": False, "error": f"checkpoint task failed: {exc}"}

    queued_devices = [
        checkpoints[device_id]["device_payload"]
        for device_id in device_ids
        if checkpoints.get(device_id, {}).get("ready")
    ]
    execution_results = execute_config_job(
        queued_devices,
        rendered_commands,
        timeout,
        max_workers=checkpoint_workers,
    )
    execution_by_device = {item.get("device_id"): item for item in execution_results}

    device_results: list[dict] = []
    all_success = True
    for device_id in device_ids:
        checkpoint = checkpoints.get(device_id) or {}
        started_at = _now_utc()
        finished_at = _now_utc()
        device_payload = devices_by_id.get(device_id) or checkpoint.get("device_payload") or {}
        device_name = checkpoint.get("device_name") or device_payload.get("name") or f"Device #{device_id}"

        if not checkpoint.get("ready"):
            all_success = False
            device_results.append(
                {
                    "device_id": device_id,
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "result": {"error": checkpoint.get("error", "pre-check failed")},
                }
            )
            continue

        push_result = execution_by_device.get(device_id) or {
            "success": False,
            "error": "job execution result missing",
        }
        if not push_result.get("success"):
            all_success = False
            error, _ = _truncate_output(push_result.get("error", ""), 4_000)
            _audit("crit", f"Apply template push failed: {device_name} - {error}", device_id)
            device_results.append(
                {
                    "device_id": device_id,
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "result": {
                        "snapshot_id": checkpoint.get("snapshot_id"),
                        "error": error,
                        "terminal_output": push_result.get("output", ""),
                    },
                }
            )
            continue

        verification = run_show_command(device_payload, "show running-config", timeout=timeout)
        if not verification.get("success"):
            all_success = False
            verify_error, _ = _truncate_output(verification.get("error", ""), 4_000)
            _audit("crit", f"Verification failed after apply for {device_name}: {verify_error}", device_id)
            device_results.append(
                {
                    "device_id": device_id,
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "result": {
                        "snapshot_id": checkpoint.get("snapshot_id"),
                        "error": f"verification failed: {verify_error}",
                        "terminal_output": push_result.get("output", ""),
                    },
                }
            )
            continue

        verified, missing_commands = _verify_required_commands(verification.get("output", ""), rendered_commands)
        if not verified:
            all_success = False
            _audit("crit", f"Verification failed after apply for {device_name}; commands missing", device_id)
            device_results.append(
                {
                    "device_id": device_id,
                    "status": "failed",
                    "started_at": started_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "result": {
                        "snapshot_id": checkpoint.get("snapshot_id"),
                        "error": "verification failed: rendered commands not found in running-config",
                        "missing_commands": missing_commands[:50],
                        "terminal_output": push_result.get("output", ""),
                    },
                }
            )
            continue

        _audit(
            "info",
            f"Template applied successfully on {device_name} (snapshot #{checkpoint.get('snapshot_id')})",
            device_id,
        )
        device_results.append(
            {
                "device_id": device_id,
                "status": "success",
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "result": {
                    "snapshot_id": checkpoint.get("snapshot_id"),
                    "template": template_name,
                    "applied_commands": rendered_commands,
                    "terminal_output": push_result.get("output", ""),
                    "prompt": push_result.get("prompt"),
                },
            }
        )

    _set_job_device_results(job_id, device_results)
    _mark_job_finished(
        job_id,
        all_success,
        {
            "job_type": job_data["type"],
            "template": template_name,
            "device_count": len(device_results),
            "device_results": device_results,
            **({} if all_success else {"error": "one or more devices failed"}),
        },
    )


def _execute_capture_snapshot(app, job_data: dict[str, Any]) -> None:
    """Executes the SSH capture and saves to ConfigSnapshot table."""
    from ..api.devices import _audit, _truncate_output
    from ..models.config_snapshot import ConfigSnapshot
    import hashlib

    job_id = int(job_data["id"])
    device_id = job_data.get("device_id")
    if device_id is None:
        _mark_job_finished(job_id, False, {"error": "job has no device_id"})
        return

    device = Device.query.get(int(device_id))
    if device is None:
        _mark_job_finished(job_id, False, {"error": "device not found"})
        return

    device_payload = serialize_device(device)
    device_name = device_payload.get("name") or f"Device #{device_id}"
    result = run_show_command(device_payload, "show running-config", timeout=20)

    if not result.get("success"):
        error_msg = result.get("error", "Unknown SSH error")
        error_truncated, _ = _truncate_output(error_msg, 4_000)
        _audit("crit", f"Snapshot capture failed for {device_name}: {error_truncated}", int(device_id))
        _mark_job_finished(
            job_id,
            False,
            {"error": error_truncated, "device_id": device_id, "device_name": device_name},
        )
        return

    config_text = result.get("output", "")
    if not config_text:
        _audit("warn", f"Snapshot capture returned empty config for {device_name}", int(device_id))
        _mark_job_finished(
            job_id,
            False,
            {"error": "SSH returned empty configuration", "device_id": device_id, "device_name": device_name},
        )
        return

    config_hash = hashlib.sha256(config_text.encode()).hexdigest()

    try:
        new_snapshot = ConfigSnapshot(
            device_id=int(device_id),
            config_text=config_text,
            config_hash=config_hash,
        )
        db.session.add(new_snapshot)
        db.session.commit()
        snapshot_id = new_snapshot.id

        _audit("info", f"Configuration snapshot captured for {device_name} (snapshot #{snapshot_id})", int(device_id))
        _mark_job_finished(
            job_id,
            True,
            {
                "snapshot_id": snapshot_id,
                "config_hash": config_hash,
                "config_size_bytes": len(config_text),
                "device_id": device_id,
                "device_name": device_name,
                "message": f"Snapshot #{snapshot_id} successfully created",
            },
        )
    except Exception as exc:
        db.session.rollback()
        error_msg = f"Failed to save snapshot: {exc}"
        current_app.logger.error(f"[JOB] Job {job_id} - {error_msg}")
        _audit("crit", f"Snapshot save failed for {device_name}: {error_msg}", int(device_id))
        _mark_job_finished(
            job_id,
            False,
            {"error": error_msg, "device_id": device_id, "device_name": device_name},
        )


def _execute_job(app, job_data: dict[str, Any]) -> None:
    job_id = int(job_data["id"])
    try:
        payload = json.loads(job_data.get("payload_json") or "{}")
    except Exception as exc:
        _mark_job_finished(job_id, False, {"error": f"invalid payload_json: {exc}"})
        return

    if job_data["type"] == "apply_template":
        _execute_apply_template_parallel(app, job_data, payload)
        return

    if job_data["type"] == "capture_snapshot":
        _execute_capture_snapshot(app, job_data)
        return

    _mark_job_finished(job_id, False, {"error": f"unsupported job type: {job_data['type']}"})


def _execute_job_by_id(app, job_id: int) -> None:
    with app.app_context():
        try:
            job_data = _claim_job(job_id)
            if job_data is None:
                return
            _execute_job(app, job_data)
        except Exception as exc:
            current_app.logger.exception(f"[JOB] Job {job_id} failed with unhandled exception")
            db.session.rollback()
            _mark_job_finished(job_id, False, {"error": f"unhandled job error: {exc}"})
        finally:
            db.session.remove()


def submit_job(job_id: int, app) -> None:
    job_executor.submit(_execute_job_by_id, app, job_id)


def poll_pending_jobs(max_jobs: int = 3) -> None:
    job_ids = (
        db.session.query(Job.id)
        .filter_by(status="pending")
        .order_by(Job.created_at.asc(), Job.id.asc())
        .limit(max_jobs)
        .all()
    )

    app = current_app._get_current_object()
    for row in job_ids:
        submit_job(row.id, app)


def start_job_worker(app) -> None:
    if scheduler.running:
        return

    with app.app_context():
        interval = app.config.get("JOB_POLL_INTERVAL_SECONDS", 3)

    def job_wrapper():
        with app.app_context():
            try:
                poll_pending_jobs()
            finally:
                db.session.remove()

    scheduler.add_job(
        job_wrapper,
        "interval",
        seconds=interval,
        id="job_worker",
        replace_existing=True,
        max_instances=1,
        misfire_grace_time=15,
        coalesce=True,
    )
    scheduler.start()
