import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from ..db import db
from ..models.device import Device
from ..models.job import Job
from .automation_service import execute_config_job
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


def _claim_job(job_id: int) -> Job | None:
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
    
    device_ids = job.get_device_ids() or [job.device_id] if job.device_id else []
    current_app.logger.info(
        f"[JOB] Job {job.id} STARTED - type={job.type}, devices={len(device_ids)}, user_id={job.user_id}"
    )
    
    return job


def _mark_job_finished(job: Job, success: bool, result: dict) -> None:
    """
    Mark job as finished and populate enterprise audit fields.
    
    Academic: Categorizes failures into Connection, Auth, Syntax, or other
    for quick dashboard viewing. Logs all job completions with severity.
    """
    job.status = "success" if success else "failed"
    job.finished_at = _now_utc()
    job.result_text = _truncate_result(json.dumps(result, default=str))
    
    device_ids = job.get_device_ids() or [job.device_id] if job.device_id else []
    device_str = f"device_ids={device_ids}" if len(device_ids) > 1 else f"device_id={job.device_id}"
    
    if not success:
        error_msg = result.get("error", "Unknown error")
        error_lower = str(error_msg).lower()
        
        # Academic: Intelligent error categorization for dashboard visibility
        if "auth" in error_lower or "password" in error_lower or "credential" in error_lower or "permission denied" in error_lower:
            job.error_summary = "Auth"  # Authentication failure
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Auth) - {device_str} - user_id={job.user_id}"
            )
        elif "timeout" in error_lower or "connection timeout" in error_lower or "connection refused" in error_lower:
            job.error_summary = "Connection"  # Connection/timeout failure
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Connection) - {device_str} - user_id={job.user_id}"
            )
        elif "syntax" in error_lower or "invalid command" in error_lower or "% invalid" in error_lower:
            job.error_summary = "Syntax"  # Command syntax error
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Syntax) - {device_str} - user_id={job.user_id}"
            )
        elif "validation" in error_lower:
            job.error_summary = "Validation Failed"
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Validation) - {device_str} - user_id={job.user_id}"
            )
        elif "template" in error_lower:
            job.error_summary = "Template Error"
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Template) - {device_str} - user_id={job.user_id}"
            )
        elif "unreachable" in error_lower or "unable to reach" in error_lower:
            job.error_summary = "Device Unreachable"
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Unreachable) - {device_str} - user_id={job.user_id}"
            )
        else:
            # Use first 100 chars of error message
            job.error_summary = (error_msg[:100] + "...") if len(str(error_msg)) > 100 else error_msg
            current_app.logger.warning(
                f"[JOB] Job {job.id} FAILED (Other) - {device_str} - user_id={job.user_id}"
            )
    else:
        current_app.logger.info(
            f"[JOB] Job {job.id} SUCCEEDED - {device_str} - user_id={job.user_id}"
        )
    
    db.session.commit()


def _capture_checkpoint_task(app, device_id: int) -> dict:
    from ..api.devices import _audit, _capture_checkpoint

    with app.app_context():
        device = Device.query.get(device_id)
        if device is None:
            return {"device_id": device_id, "ready": False, "error": "device not found"}

        pre_ok, snapshot, pre_error = _capture_checkpoint(device)
        if not pre_ok or snapshot is None:
            _audit("crit", f"Apply template failed (pre-check): {device.name} - {pre_error}", device.id)
            return {
                "device_id": device_id,
                "ready": False,
                "error": pre_error,
                "device_name": device.name,
            }

        _audit("info", f"Checkpoint saved for {device.name} (snapshot #{snapshot.id})", device.id)
        return {
            "device_id": device_id,
            "ready": True,
            "snapshot_id": snapshot.id,
            "pre_config": snapshot.config_text,
            "device_name": device.name,
            "device_payload": {
                "id": device.id,
                "name": device.name,
                "ip_address": device.ip_address,
                "vendor": device.vendor,
                "device_type": device.device_type,
            },
        }


def _execute_apply_template_parallel(app, job: Job, payload: dict) -> None:
    from ..api.devices import _audit, _capture_checkpoint, _truncate_output, _verify_required_commands
    from ..services.template_engine import TemplateNotAvailableError, TemplateRenderError, render_template_commands
    from ..services.validator import validate_commands

    template_name = (payload.get("template") or "").strip()
    variables = payload.get("variables") or {}
    allow_risky_commands = bool(payload.get("allow_risky_commands", False))
    timeout = int(app.config.get("AUTOMATION_TIMEOUT_SECONDS", 20))

    if not template_name:
        with app.app_context():
            current_app.logger.error(f"[JOB] Job {job.id} - Template name is required")
        _mark_job_finished(job, False, {"error": "payload.template is required"})
        return

    try:
        with app.app_context():
            current_app.logger.info(f"[JOB] Job {job.id} - Rendering template: {template_name}")
        rendered_commands = render_template_commands(template_name, variables)
        with app.app_context():
            current_app.logger.info(f"[JOB] Job {job.id} - Template rendered successfully ({len(rendered_commands)} commands)")
    except TemplateNotAvailableError as exc:
        with app.app_context():
            current_app.logger.error(f"[JOB] Job {job.id} - Template not available: {exc}")
        _mark_job_finished(job, False, {"error": str(exc)})
        return
    except TemplateRenderError as exc:
        with app.app_context():
            current_app.logger.error(f"[JOB] Job {job.id} - Template render failed: {exc}")
        _mark_job_finished(job, False, {"error": f"template render failed: {exc}"})
        return

    with app.app_context():
        current_app.logger.info(f"[JOB] Job {job.id} - Validating rendered commands")
    
    validation_result = validate_commands(
        rendered_commands,
        variables=variables,
        allow_risky_commands=allow_risky_commands,
    )
    if not validation_result.get("passed"):
        with app.app_context():
            current_app.logger.error(f"[JOB] Job {job.id} - Command validation failed: {validation_result.get('reasons', [])}")
        _mark_job_finished(job, False, {"error": "validation failed", "validation": validation_result})
        return

    device_ids = job.get_device_ids()
    devices = Device.query.filter(Device.id.in_(device_ids)).all()
    devices_by_id = {device.id: device for device in devices}

    checkpoints: dict[int, dict] = {device_id: {"ready": False, "error": "device not found"} for device_id in device_ids}
    worker_limit = app.config.get("AUTOMATION_MAX_WORKERS", 10)
    checkpoint_workers = max(1, min(int(worker_limit), len(device_ids) or 1))

    with app.app_context():
        current_app.logger.info(f"[JOB] Job {job.id} - Starting parallel execution on {len(device_ids)} devices")

    with ThreadPoolExecutor(max_workers=checkpoint_workers, thread_name_prefix="job-checkpoint") as executor:
        future_map = {executor.submit(_capture_checkpoint_task, app, device_id): device_id for device_id in device_ids}
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

        execution_future = executor.submit(
            execute_config_job,
            queued_devices,
            rendered_commands,
            timeout,
            app.config.get("AUTOMATION_MAX_WORKERS", 10),
        )
        execution_results = execution_future.result()

    execution_by_device = {item.get("device_id"): item for item in execution_results}

    device_results: list[dict] = []
    all_success = True
    for device_id in device_ids:
        checkpoint = checkpoints.get(device_id) or {}
        started_at = _now_utc()
        finished_at = _now_utc()
        device = devices_by_id.get(device_id)
        device_name = checkpoint.get("device_name") or (device.name if device else f"Device #{device_id}")

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

        push_result = execution_by_device.get(device_id) or {"success": False, "error": "job execution result missing"}
        if not push_result.get("success"):
            all_success = False
            error, _ = _truncate_output(push_result.get("error", ""), 4_000)
            if device is not None:
                _audit("crit", f"Apply template push failed: {device_name} - {error}", device.id)
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

        verification = run_show_command(device or checkpoint.get("device_payload"), "show running-config", timeout=timeout)
        if not verification.get("success"):
            all_success = False
            verify_error, _ = _truncate_output(verification.get("error", ""), 4_000)
            if device is not None:
                _audit("crit", f"Verification failed after apply for {device_name}: {verify_error}", device.id)
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
            if device is not None:
                _audit("crit", f"Verification failed after apply for {device_name}; commands missing", device.id)
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

        if device is not None:
            _audit("info", f"Template applied successfully on {device_name} (snapshot #{checkpoint.get('snapshot_id')})", device.id)
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

    job.set_device_results(device_results)
    db.session.commit()
    _mark_job_finished(
        job,
        all_success,
        {
            "job_type": job.type,
            "template": template_name,
            "device_count": len(device_results),
            "device_results": device_results,
        },
    )


def _execute_job(app, job: Job) -> None:
    try:
        payload = json.loads(job.payload_json or "{}")
    except Exception as exc:
        _mark_job_finished(job, False, {"error": f"invalid payload_json: {exc}"})
        return

    if job.type == "apply_template":
        _execute_apply_template_parallel(app, job, payload)
        return

    _mark_job_finished(job, False, {"error": f"unsupported job type: {job.type}"})


def _execute_job_by_id(app, job_id: int) -> None:
    with app.app_context():
        job = _claim_job(job_id)
        if job is None:
            return
        _execute_job(app, job)


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
            poll_pending_jobs()

    scheduler.add_job(job_wrapper, "interval", seconds=interval, id="job_worker", replace_existing=True)
    scheduler.start()
