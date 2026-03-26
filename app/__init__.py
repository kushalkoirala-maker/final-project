import os
import json
from datetime import datetime, timedelta
from flask import Flask
from dotenv import load_dotenv
from sqlalchemy import inspect, text
from .config import Config
from .db import db
from .auth import login_manager
from .services.monitor import start_monitor
from .services.job_worker import start_job_worker
from .services.snmp_monitor import start_snmp_monitor


def _is_demo_mode_enabled() -> bool:
    value = (os.getenv("DEMO_MODE") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _seed_demo_data_if_needed() -> None:
    from .models.device import Device
    from .models.job import Job
    from .models.alert import Alert

    if Device.query.count() == 0:
        devices = [
            Device(
                name="R1-Demo",
                ip_address="192.0.2.11",
                device_type="router",
                vendor="cisco",
                location="EVE-NG Pod A",
            ),
            Device(
                name="SW1-Demo",
                ip_address="192.0.2.21",
                device_type="switch",
                vendor="cisco",
                location="EVE-NG Pod A",
            ),
            Device(
                name="R2-Demo",
                ip_address="192.0.2.12",
                device_type="router",
                vendor="cisco",
                location="EVE-NG Pod B",
            ),
        ]
        db.session.add_all(devices)
        db.session.commit()

    # Seed optional demo jobs/alerts only when those tables are empty.
    first_device = Device.query.order_by(Device.id.asc()).first()
    if first_device is not None and Job.query.count() == 0:
        now = datetime.utcnow()
        jobs = [
            Job(
                device_id=first_device.id,
                type="apply_template",
                payload_json=json.dumps({"template": "vlan_creation", "variables": {"vlan_id": 10, "vlan_name": "USERS"}}),
                status="success",
                started_at=now - timedelta(minutes=3),
                finished_at=now - timedelta(minutes=2),
                result_text="Demo: template applied successfully.",
            ),
            Job(
                device_id=first_device.id,
                type="apply_template",
                payload_json=json.dumps({"template": "static_route", "variables": {"destination_network": "10.50.0.0"}}),
                status="running",
                started_at=now - timedelta(seconds=25),
                result_text="Demo: pushing configuration...",
            ),
            Job(
                device_id=first_device.id,
                type="apply_template",
                payload_json=json.dumps({"template": "trunk_setup", "variables": {"interface": "Gi1/0/48"}}),
                status="pending",
                result_text="Demo: queued.",
            ),
        ]
        db.session.add_all(jobs)
        db.session.commit()

    if first_device is not None and Alert.query.count() == 0:
        alerts = [
            Alert(severity="info", message="Demo monitor initialized.", device_id=first_device.id),
            Alert(severity="warn", message="Demo interface flap detected.", device_id=first_device.id),
            Alert(severity="crit", message="Demo high CPU threshold exceeded.", device_id=first_device.id),
        ]
        db.session.add_all(alerts)
        db.session.commit()


def _ensure_job_table_columns() -> None:
    inspector = inspect(db.engine)
    table_names = set(inspector.get_table_names())
    if "job" not in table_names:
        return

    existing_columns = {col["name"] for col in inspector.get_columns("job")}
    ddl = []
    if "device_ids_json" not in existing_columns:
        ddl.append("ALTER TABLE job ADD COLUMN device_ids_json TEXT")
    if "device_results_json" not in existing_columns:
        ddl.append("ALTER TABLE job ADD COLUMN device_results_json TEXT")

    for statement in ddl:
        db.session.execute(text(statement))
    if ddl:
        db.session.commit()


def create_app():
    load_dotenv()

    app = Flask(__name__, instance_relative_config=True)
    app.config.from_object(Config)

    # Ensure instance folder exists
    os.makedirs(app.instance_path, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)

    from .routes import web_bp
    from .api.devices import api_devices_bp

    app.register_blueprint(web_bp)
    app.register_blueprint(api_devices_bp, url_prefix="/api")

    with app.app_context():
        from .models import user, device, alert, config_snapshot, job, metrics  # noqa: F401
        db.create_all()
        _ensure_job_table_columns()

        # Create a default admin if none exists
        from .models.user import User
        if User.query.count() == 0:
            admin = User.create_admin_default()
            db.session.add(admin)
            db.session.commit()

        if _is_demo_mode_enabled():
            _seed_demo_data_if_needed()

    # Start background services
    start_monitor(app)
    start_job_worker(app)
    start_snmp_monitor(app)

    return app
