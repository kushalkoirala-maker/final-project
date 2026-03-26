from pathlib import Path

from flask import Blueprint, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import and_, func

from .db import db
from .models.alert import Alert
from .models.config_snapshot import ConfigSnapshot
from .models.device import Device
from .models.job import Job
from .models.metrics import Metrics
from .models.user import User
from .services.monitor import fetch_monitoring_snapshot
from .services.template_engine import TEMPLATE_MAP, available_templates, template_schema_map

web_bp = Blueprint("web", __name__)
MAX_SNAPSHOT_PREVIEW_CHARS = 120_000


def _build_monitoring_payload() -> list[dict]:
    devices = Device.query.order_by(Device.name.asc()).all()

    latest_subquery = (
        db.session.query(
            Metrics.device_id.label("device_id"),
            Metrics.metric_name.label("metric_name"),
            func.max(Metrics.timestamp).label("max_timestamp"),
        )
        .group_by(Metrics.device_id, Metrics.metric_name)
        .subquery()
    )

    latest_metrics = (
        db.session.query(Metrics)
        .join(
            latest_subquery,
            and_(
                Metrics.device_id == latest_subquery.c.device_id,
                Metrics.metric_name == latest_subquery.c.metric_name,
                Metrics.timestamp == latest_subquery.c.max_timestamp,
            ),
        )
        .all()
    )

    metric_map: dict[int, dict[str, float]] = {}
    for item in latest_metrics:
        metric_map.setdefault(item.device_id, {})[item.metric_name] = item.value

    device_health = []
    for device in devices:
        metrics = metric_map.get(device.id, {})

        interfaces = []
        for index in range(1, 49):
            status_key = f"if_oper_status_{index}"
            in_key = f"if_in_octets_{index}"
            out_key = f"if_out_octets_{index}"

            status = metrics.get(status_key)
            in_octets = metrics.get(in_key)
            out_octets = metrics.get(out_key)

            if status is None and in_octets is None and out_octets is None:
                continue

            interfaces.append(
                {
                    "index": index,
                    "status": int(status) if status is not None else None,
                    "is_up": status == 1,
                    "in_octets": round(float(in_octets), 2) if in_octets is not None else None,
                    "out_octets": round(float(out_octets), 2) if out_octets is not None else None,
                }
            )

        device_health.append(
            {
                "device_id": device.id,
                "device_name": device.name,
                "ip_address": device.ip_address,
                "cpu_usage": round(float(metrics["cpu_usage"]), 2) if "cpu_usage" in metrics else None,
                "memory_usage": round(float(metrics["memory_usage"]), 2) if "memory_usage" in metrics else None,
                "uptime_ticks": int(round(float(metrics["uptime_ticks"]))) if "uptime_ticks" in metrics else None,
                "interfaces": interfaces,
            }
        )

    return device_health


def _require_admin_web():
    if current_user.role != "admin":
        abort(403)
    return None


def _create_user_from_form() -> tuple[bool, str]:
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    role = (request.form.get("role") or "viewer").strip()

    if role not in ("admin", "operator", "viewer"):
        return False, "Invalid role."
    if not username or len(password) < 8:
        return False, "Username and password (min 8 chars) are required."
    if User.query.filter_by(username=username).first():
        return False, "Username already exists."

    user = User(username=username, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return True, f"User {username} created."


@web_bp.get("/")
def home():
    if current_user.is_authenticated:
        return redirect(url_for("web.dashboard"))
    return redirect(url_for("web.login"))


@web_bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            flash("Invalid username or password", "danger")
            return redirect(url_for("web.login"))
        if user.is_disabled:
            flash("Your account is disabled. Contact an administrator.", "danger")
            return redirect(url_for("web.login"))

        login_user(user)
        return redirect(url_for("web.dashboard"))

    return render_template("pages/login.html")


@web_bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("web.login"))


@web_bp.get("/dashboard")
@login_required
def dashboard():
    devices = Device.query.order_by(Device.name.asc()).all()
    monitor_payload = fetch_monitoring_snapshot(devices=devices, update_inventory=True)
    zabbix_connected = monitor_payload.get("source") == "zabbix"
    summary = monitor_payload.get("summary", {})
    total_devices = summary.get("total_devices", len(devices))
    up_devices = summary.get("devices_online", sum(1 for device in devices if device.is_up))
    failed_jobs = Job.query.filter_by(status="failed").count()
    active_zabbix_alerts = summary.get("active_zabbix_alerts", 0)
    alerts = Alert.query.order_by(Alert.created_at.desc()).limit(10).all()
    recent_jobs = Job.query.order_by(Job.created_at.desc()).limit(5).all()
    dashboard_device_ids = sorted({d for j in recent_jobs for d in j.get_device_ids()})
    dashboard_devices = {d.id: d for d in Device.query.filter(Device.id.in_(dashboard_device_ids)).all()} if dashboard_device_ids else {}

    recent_job_rows = []
    for job in recent_jobs:
        results = {item.get("device_id"): item for item in job.get_device_results()}
        per_device = []
        for did in job.get_device_ids():
            per_device.append(
                {
                    "device": dashboard_devices.get(did),
                    "device_id": did,
                    "status": (results.get(did) or {}).get("status", job.status),
                }
            )
        recent_job_rows.append({"job": job, "per_device": per_device})

    return render_template(
        "pages/dashboard.html",
        total_devices=total_devices,
        up_devices=up_devices,
        failed_jobs=failed_jobs,
        active_zabbix_alerts=active_zabbix_alerts,
        zabbix_connected=zabbix_connected,
        monitored_hosts=monitor_payload.get("hosts", []),
        monitor_warning=monitor_payload.get("warning"),
        alerts=alerts,
        recent_jobs=recent_jobs,
        recent_job_rows=recent_job_rows,
    )


@web_bp.get("/monitoring")
@login_required
def monitoring_page():
    monitor_payload = fetch_monitoring_snapshot(update_inventory=False)
    return render_template(
        "monitoring.html",
        device_health=_build_monitoring_payload(),
        zabbix_connected=monitor_payload.get("source") == "zabbix",
        monitor_warning=monitor_payload.get("warning"),
    )


@web_bp.get("/api/monitoring")
@login_required
def monitoring_api():
    monitor_payload = fetch_monitoring_snapshot(update_inventory=False)
    return jsonify(
        {
            "devices": _build_monitoring_payload(),
            "zabbix_connected": monitor_payload.get("source") == "zabbix",
            "warning": monitor_payload.get("warning"),
        }
    )


@web_bp.route("/devices", methods=["GET", "POST"])
@login_required
def devices():
    if request.method == "POST":
        if current_user.role not in ("admin", "operator"):
            flash("You don't have permission to add devices.", "danger")
            return redirect(url_for("web.devices"))

        name = request.form.get("name", "").strip()
        ip = request.form.get("ip_address", "").strip()
        device_type = request.form.get("device_type", "router")
        vendor = request.form.get("vendor", "cisco")
        location = request.form.get("location", "").strip() or None

        if not name or not ip:
            flash("Name and IP are required.", "danger")
            return redirect(url_for("web.devices"))

        if Device.query.filter_by(ip_address=ip).first():
            flash("IP already exists in inventory.", "danger")
            return redirect(url_for("web.devices"))

        d = Device(name=name, ip_address=ip, device_type=device_type, vendor=vendor, location=location)
        db.session.add(d)
        db.session.commit()
        flash("Device added.", "success")
        return redirect(url_for("web.devices"))

    inventory = Device.query.order_by(Device.created_at.desc()).all()
    monitor_payload = fetch_monitoring_snapshot(devices=inventory, update_inventory=True)
    status_map = {item.get("device_id"): item for item in monitor_payload.get("hosts", [])}

    return render_template(
        "pages/devices.html",
        devices=inventory,
        monitor_warning=monitor_payload.get("warning"),
        status_map=status_map,
    )


@web_bp.post("/devices/<int:device_id>/delete")
@login_required
def device_delete(device_id: int):
    """Hard-delete a device and its related rows.

    Models do not define ORM cascades, and SQLite FK behavior can vary
    depending on configuration. We therefore delete related rows explicitly.
    """
    if current_user.role not in ("admin", "operator"):
        flash("You don't have permission to delete devices.", "danger")
        return redirect(url_for("web.devices"))

    device = Device.query.get(device_id)
    if device is None:
        abort(404)

    Alert.query.filter_by(device_id=device.id).delete(synchronize_session=False)
    ConfigSnapshot.query.filter_by(device_id=device.id).delete(synchronize_session=False)
    Job.query.filter_by(device_id=device.id).delete(synchronize_session=False)
    db.session.delete(device)
    db.session.commit()
    flash(f"Device {device.name} removed.", "success")
    return redirect(url_for("web.devices"))


@web_bp.get("/devices/<int:device_id>")
@login_required
def device_detail(device_id: int):
    device = Device.query.get(device_id)
    if device is None:
        abort(404)

    can_run_commands = current_user.role in ("admin", "operator")
    return render_template(
        "pages/device_detail.html",
        device=device,
        can_run_commands=can_run_commands,
    )


@web_bp.get("/devices/<int:device_id>/snapshots")
@login_required
def device_snapshots(device_id: int):
    device = Device.query.get(device_id)
    if device is None:
        abort(404)

    snapshots = (
        ConfigSnapshot.query.filter_by(device_id=device.id)
        .order_by(ConfigSnapshot.created_at.desc(), ConfigSnapshot.id.desc())
        .all()
    )
    return render_template("pages/device_snapshots.html", device=device, snapshots=snapshots)


@web_bp.get("/snapshots/<int:snapshot_id>")
@login_required
def snapshot_detail(snapshot_id: int):
    snapshot = ConfigSnapshot.query.get(snapshot_id)
    if snapshot is None:
        abort(404)

    device = Device.query.get(snapshot.device_id)
    config_text = snapshot.config_text or ""
    truncated = False
    if len(config_text) > MAX_SNAPSHOT_PREVIEW_CHARS:
        config_text = config_text[:MAX_SNAPSHOT_PREVIEW_CHARS] + "\n\n... [truncated in UI]"
        truncated = True

    return render_template(
        "pages/snapshot_view.html",
        snapshot=snapshot,
        device=device,
        config_text=config_text,
        truncated=truncated,
    )


@web_bp.get("/templates")
@login_required
def templates_page():
    template_names = available_templates()
    schemas = template_schema_map()
    template_sources: dict[str, str] = {}
    templates_root = Path(web_bp.root_path) / "templates" / "config_templates"
    for name, filename in TEMPLATE_MAP.items():
        try:
            with open(templates_root / filename, "r", encoding="utf-8") as handle:
                template_sources[name] = handle.read()
        except Exception:
            template_sources[name] = ""
    return render_template(
        "pages/templates.html",
        templates=template_names,
        template_schemas=schemas,
        template_sources=template_sources,
        can_apply=current_user.role in ("admin", "operator"),
    )


@web_bp.get("/templates/apply")
@login_required
def templates_apply_page():
    devices = Device.query.order_by(Device.name.asc()).all()
    template_names = available_templates()
    schemas = template_schema_map()
    selected_device_id = request.args.get("device_id", type=int)
    return render_template(
        "pages/template_apply.html",
        devices=devices,
        templates=template_names,
        template_schemas=schemas,
        selected_device_id=selected_device_id,
        can_apply=current_user.role in ("admin", "operator"),
    )


@web_bp.get("/jobs")
@login_required
def jobs_page():
    status = (request.args.get("status") or "").strip().lower()
    device_id = request.args.get("device_id", type=int)
    job_id = request.args.get("job_id", type=int)

    query = Job.query
    if status in ("pending", "running", "success", "failed"):
        query = query.filter_by(status=status)
    if job_id is not None:
        query = query.filter_by(id=job_id)

    jobs = query.order_by(Job.created_at.desc(), Job.id.desc()).limit(300).all()
    if device_id is not None:
        jobs = [j for j in jobs if j.has_device(device_id)]
    jobs = jobs[:50]

    resolved_device_ids = sorted({d for j in jobs for d in j.get_device_ids()})
    devices = {d.id: d for d in Device.query.filter(Device.id.in_(resolved_device_ids)).all()} if resolved_device_ids else {}
    all_devices = [
        {"id": d.id, "name": d.name, "ip_address": d.ip_address}
        for d in Device.query.order_by(Device.name.asc()).all()
    ]

    job_rows = []
    for job in jobs:
        per_device = []
        result_by_device = {item.get("device_id"): item for item in job.get_device_results()}
        for did in job.get_device_ids():
            per_device.append(
                {
                    "device_id": did,
                    "device": devices.get(did),
                    "status": (result_by_device.get(did) or {}).get("status", "pending" if job.status == "pending" else "running" if job.status == "running" else "unknown"),
                    "started_at": (result_by_device.get(did) or {}).get("started_at"),
                    "finished_at": (result_by_device.get(did) or {}).get("finished_at"),
                }
            )
        job_rows.append({"job": job, "per_device": per_device})

    return render_template(
        "pages/jobs.html",
        jobs=jobs,
        job_rows=job_rows,
        devices=devices,
        all_devices=all_devices,
        templates=available_templates(),
        template_schemas=template_schema_map(),
        filters={"status": status, "device_id": device_id, "job_id": job_id},
    )


@web_bp.get("/jobs/<int:job_id>")
@login_required
def job_detail_page(job_id: int):
    job = Job.query.get(job_id)
    if job is None:
        abort(404)

    device_ids = job.get_device_ids()
    devices = {d.id: d for d in Device.query.filter(Device.id.in_(device_ids)).all()} if device_ids else {}
    result_by_device = {item.get("device_id"): item for item in job.get_device_results()}
    per_device = []
    for did in device_ids:
        per_device.append(
            {
                "device_id": did,
                "device": devices.get(did),
                "status": (result_by_device.get(did) or {}).get("status", "pending" if job.status == "pending" else "running" if job.status == "running" else "unknown"),
                "started_at": (result_by_device.get(did) or {}).get("started_at"),
                "finished_at": (result_by_device.get(did) or {}).get("finished_at"),
                "result": (result_by_device.get(did) or {}).get("result"),
            }
        )

    return render_template("pages/job_detail.html", job=job, devices=devices, per_device=per_device)


@web_bp.get("/alerts")
@login_required
def alerts_page():
    severity = (request.args.get("severity") or "").strip().lower()
    device_id = request.args.get("device_id", type=int)
    limit = min(max(int(request.args.get("limit", 200)), 1), 500)

    query = Alert.query
    if severity in ("info", "warn", "crit"):
        query = query.filter_by(severity=severity)
    if device_id is not None:
        query = query.filter_by(device_id=device_id)

    alerts = query.order_by(Alert.created_at.desc(), Alert.id.desc()).limit(limit).all()
    devices = Device.query.order_by(Device.name.asc()).all()
    device_map = {d.id: d for d in devices}
    return render_template(
        "pages/alerts.html",
        alerts=alerts,
        devices=devices,
        device_map=device_map,
        filters={"severity": severity, "device_id": device_id, "limit": limit},
    )


@web_bp.post("/alerts/clear")
@login_required
def alerts_clear():
    """Clear alerts (admin-only). Supports clearing all or clearing filtered."""
    if current_user.role != "admin":
        flash("Only admin can clear alerts.", "danger")
        return redirect(url_for("web.alerts_page"))

    scope = (request.form.get("scope") or "filtered").strip().lower()
    severity = (request.form.get("severity") or "").strip().lower()
    device_id = request.form.get("device_id", type=int)
    limit = request.form.get("limit", 200)

    query = Alert.query
    if scope != "all":
        if severity in ("info", "warn", "crit"):
            query = query.filter_by(severity=severity)
        if device_id is not None:
            query = query.filter_by(device_id=device_id)

    deleted = query.delete(synchronize_session=False)
    db.session.commit()
    flash(f"Cleared {deleted} alert(s).", "success")
    return redirect(url_for("web.alerts_page", severity=severity, device_id=device_id, limit=limit))


@web_bp.route("/admin/users", methods=["GET", "POST"])
@login_required
def admin_users_page():
    guard = _require_admin_web()
    if guard is not None:
        return guard

    if request.method == "POST":
        ok, message = _create_user_from_form()
        flash(message, "success" if ok else "danger")
        return redirect(url_for("web.admin_users_page"))

    users = User.query.order_by(User.username.asc()).all()
    return render_template("pages/admin_users.html", users=users)


@web_bp.post("/admin/users/create")
@login_required
def admin_user_create():
    guard = _require_admin_web()
    if guard is not None:
        return guard

    ok, message = _create_user_from_form()
    flash(message, "success" if ok else "danger")
    return redirect(url_for("web.admin_users_page"))


@web_bp.post("/admin/users/<int:user_id>/role")
@login_required
def admin_user_update_role(user_id: int):
    guard = _require_admin_web()
    if guard is not None:
        return guard

    user = User.query.get(user_id)
    if user is None:
        abort(404)

    role = (request.form.get("role") or "").strip()
    if role not in ("admin", "operator", "viewer"):
        flash("Invalid role.", "danger")
        return redirect(url_for("web.admin_users_page"))

    user.role = role
    db.session.commit()
    flash(f"Role updated for {user.username}.", "success")
    return redirect(url_for("web.admin_users_page"))


@web_bp.post("/admin/users/<int:user_id>/toggle")
@login_required
def admin_user_toggle(user_id: int):
    guard = _require_admin_web()
    if guard is not None:
        return guard

    user = User.query.get(user_id)
    if user is None:
        abort(404)

    if user.id == current_user.id and user.role != "disabled":
        flash("You cannot disable your own account.", "danger")
        return redirect(url_for("web.admin_users_page"))

    if user.role == "disabled":
        user.role = "viewer"
        db.session.commit()
        flash(f"User {user.username} enabled with viewer role.", "success")
        return redirect(url_for("web.admin_users_page"))

    user.role = "disabled"
    db.session.commit()
    flash(f"User {user.username} disabled.", "success")
    return redirect(url_for("web.admin_users_page"))


@web_bp.post("/admin/users/<int:user_id>/delete")
@login_required
def admin_user_delete(user_id: int):
    guard = _require_admin_web()
    if guard is not None:
        return guard

    user = User.query.get(user_id)
    if user is None:
        abort(404)

    if user.id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for("web.admin_users_page"))

    if user.role == "admin":
        admin_count = User.query.filter_by(role="admin").count()
        if admin_count <= 1:
            flash("Cannot delete the last admin account.", "danger")
            return redirect(url_for("web.admin_users_page"))

    username = user.username
    db.session.delete(user)
    db.session.commit()
    flash(f"User {username} deleted.", "success")
    return redirect(url_for("web.admin_users_page"))


@web_bp.post("/admin/users/<int:user_id>/disable")
@login_required
def admin_user_disable(user_id: int):
    guard = _require_admin_web()
    if guard is not None:
        return guard

    user = User.query.get(user_id)
    if user is None:
        abort(404)

    if user.id == current_user.id:
        flash("You cannot disable your own account.", "danger")
        return redirect(url_for("web.admin_users_page"))

    user.role = "disabled"
    db.session.commit()
    flash(f"User {user.username} disabled.", "success")
    return redirect(url_for("web.admin_users_page"))


@web_bp.post("/admin/users/<int:user_id>/enable")
@login_required
def admin_user_enable(user_id: int):
    guard = _require_admin_web()
    if guard is not None:
        return guard

    user = User.query.get(user_id)
    if user is None:
        abort(404)

    if user.role != "disabled":
        flash("User is already enabled.", "info")
        return redirect(url_for("web.admin_users_page"))

    user.role = "viewer"
    db.session.commit()
    flash(f"User {user.username} enabled with viewer role.", "success")
    return redirect(url_for("web.admin_users_page"))
