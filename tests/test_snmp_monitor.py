import pytest

from app import create_app
from app.config import Config
from app.db import db
from app.models.device import Device
from app.models.metrics import Metrics
from app.services import job_worker, monitor, snmp_monitor


@pytest.fixture()
def app_ctx(tmp_path):
    db_path = tmp_path / "test_snmp.db"
    Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
    Config.JOB_POLL_INTERVAL_SECONDS = 3600
    Config.MONITOR_INTERVAL_SECONDS = 3600
    Config.SNMP_POLL_INTERVAL_SECONDS = 3600

    app = create_app()
    app.config.update(TESTING=True)

    with app.app_context():
        yield app

    try:
        if monitor.scheduler.running:
            monitor.scheduler.shutdown(wait=False)
    except Exception:
        pass
    try:
        if job_worker.scheduler.running:
            job_worker.scheduler.shutdown(wait=False)
    except Exception:
        pass
    try:
        if snmp_monitor.scheduler.running:
            snmp_monitor.scheduler.shutdown(wait=False)
    except Exception:
        pass


def test_poll_all_devices_writes_metrics(app_ctx, monkeypatch):
    with app_ctx.app_context():
        device = Device(name="R1", ip_address="10.0.0.1", device_type="router", vendor="cisco")
        db.session.add(device)
        db.session.commit()
        device_id = device.id

    def fake_poll_device_metrics(device, community, timeout_seconds=2, retries=1):
        assert device.id == device_id
        assert community == app_ctx.config["SNMP_COMMUNITY"]
        return [
            {"metric_name": "cpu_usage", "value": 42.5},
            {"metric_name": "memory_usage", "value": 67.0},
            {"metric_name": "uptime_ticks", "value": 123456.0},
            {"metric_name": "if_oper_status_1", "value": 1},
            {"metric_name": "if_in_octets_1", "value": 1000},
            {"metric_name": "if_out_octets_1", "value": 1500},
        ]

    monkeypatch.setattr(snmp_monitor, "poll_device_metrics", fake_poll_device_metrics)

    with app_ctx.app_context():
        written = snmp_monitor.poll_all_devices()
        assert written == 6

        rows = Metrics.query.filter_by(device_id=device_id).all()
        assert len(rows) == 6
        names = sorted(row.metric_name for row in rows)
        assert names == [
            "cpu_usage",
            "if_in_octets_1",
            "if_oper_status_1",
            "if_out_octets_1",
            "memory_usage",
            "uptime_ticks",
        ]


def test_poll_all_devices_handles_no_devices(app_ctx):
    with app_ctx.app_context():
        written = snmp_monitor.poll_all_devices()
        assert written == 0
