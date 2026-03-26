import pytest

from app import create_app
from app.config import Config
from app.db import db
from app.models.alert import Alert
from app.models.config_snapshot import ConfigSnapshot
from app.models.device import Device
from app.models.user import User
from app.services import job_worker, monitor, snmp_monitor


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "test_netops.db"
    Config.SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
    Config.JOB_POLL_INTERVAL_SECONDS = 3600
    Config.MONITOR_INTERVAL_SECONDS = 3600

    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as test_client:
        yield test_client

    # Cleanly stop background schedulers started by app factory.
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


def _login_admin(client):
    return client.post(
        "/login",
        data={"username": "admin", "password": "admin123"},
        follow_redirects=False,
    )


def _create_and_login_viewer(client):
    with client.application.app_context():
        viewer = User(username="viewer1", role="viewer")
        viewer.set_password("viewer123")
        db.session.add(viewer)
        db.session.commit()
    return client.post(
        "/login",
        data={"username": "viewer1", "password": "viewer123"},
        follow_redirects=False,
    )


def _seed_device_snapshot_alert():
    device = Device(name="R1", ip_address="10.0.0.1", device_type="router", vendor="cisco")
    db.session.add(device)
    db.session.commit()

    snapshot = ConfigSnapshot(
        device_id=device.id,
        config_text="hostname R1\ninterface Lo0\n ip address 1.1.1.1 255.255.255.255\n",
        config_hash="abc123",
    )
    alert = Alert(severity="crit", message="Device DOWN", device_id=device.id)
    db.session.add(snapshot)
    db.session.add(alert)
    db.session.commit()
    return device, snapshot, alert


def test_templates_schema_endpoint(client):
    _login_admin(client)
    response = client.get("/api/templates/schema")
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert "vlan_creation" in data["templates"]
    assert "schemas" in data


def test_alerts_and_snapshots_endpoints(client):
    _login_admin(client)
    with client.application.app_context():
        device, snapshot, _ = _seed_device_snapshot_alert()
        device_id = device.id
        snapshot_id = snapshot.id

    alerts_resp = client.get(f"/api/alerts?severity=crit&device_id={device_id}&limit=10")
    assert alerts_resp.status_code == 200
    alerts_data = alerts_resp.get_json()
    assert len(alerts_data) >= 1
    assert alerts_data[0]["severity"] == "crit"

    list_resp = client.get(f"/api/devices/{device_id}/snapshots")
    assert list_resp.status_code == 200
    list_data = list_resp.get_json()
    assert any(item["id"] == snapshot_id for item in list_data)

    detail_resp = client.get(f"/api/snapshots/{snapshot_id}")
    assert detail_resp.status_code == 200
    detail_data = detail_resp.get_json()
    assert detail_data["id"] == snapshot_id
    assert "hostname R1" in detail_data["config_text"]


def test_apply_template_queues_job_and_jobs_list(client):
    _login_admin(client)
    with client.application.app_context():
        device = Device(name="R2", ip_address="10.0.0.2", device_type="switch", vendor="cisco")
        db.session.add(device)
        db.session.commit()
        device_id = device.id

    queue_resp = client.post(
        f"/api/devices/{device_id}/apply_template",
        json={"template": "vlan_creation", "variables": {"vlan_id": 10, "vlan_name": "OPS"}},
    )
    assert queue_resp.status_code == 202
    queue_data = queue_resp.get_json()
    assert queue_data["success"] is True
    job_id = queue_data["job"]["id"]

    jobs_resp = client.get("/api/jobs?status=pending&limit=20")
    assert jobs_resp.status_code == 200
    jobs_data = jobs_resp.get_json()
    assert any(job["id"] == job_id for job in jobs_data)

    status_resp = client.get(f"/api/jobs/{job_id}")
    assert status_resp.status_code == 200
    status_data = status_resp.get_json()
    assert status_data["success"] is True
    assert status_data["job"]["id"] == job_id


def test_create_multi_device_job_and_filter_by_device(client):
    _login_admin(client)
    with client.application.app_context():
        d1 = Device(name="R10", ip_address="10.0.10.1", device_type="router", vendor="cisco")
        d2 = Device(name="R11", ip_address="10.0.10.2", device_type="router", vendor="cisco")
        db.session.add(d1)
        db.session.add(d2)
        db.session.commit()

        d1_id = d1.id
        d2_id = d2.id

    create_resp = client.post(
        "/api/jobs",
        json={
            "type": "apply_template",
            "device_ids": [d1_id, d2_id],
            "payload": {"template": "vlan_creation", "variables": {"vlan_id": 20, "vlan_name": "OPS"}},
        },
    )
    assert create_resp.status_code == 201
    create_data = create_resp.get_json()
    assert create_data["success"] is True
    assert set(create_data["job"]["device_ids"]) == {d1_id, d2_id}

    list_resp = client.get(f"/api/jobs?device_id={d2_id}&limit=20")
    assert list_resp.status_code == 200
    jobs = list_resp.get_json()
    assert len(jobs) >= 1
    assert any(d2_id in job.get("device_ids", []) for job in jobs)


def test_viewer_can_preview_and_diff_but_cannot_apply(client, monkeypatch):
    _create_and_login_viewer(client)
    with client.application.app_context():
        device = Device(name="R3", ip_address="10.0.0.3", device_type="router", vendor="cisco")
        db.session.add(device)
        db.session.commit()
        device_id = device.id
        snapshot = ConfigSnapshot(
            device_id=device.id,
            config_text="hostname R3\n",
            config_hash="hash-r3",
        )
        alert = Alert(severity="info", message="Viewer-visible alert", device_id=device.id)
        db.session.add(snapshot)
        db.session.add(alert)
        db.session.commit()
        snapshot_id = snapshot.id

    preview_resp = client.post(
        "/api/templates/preview",
        json={"template": "vlan_creation", "variables": {"vlan_id": 10, "vlan_name": "OPS"}},
    )
    assert preview_resp.status_code == 200
    assert preview_resp.get_json()["success"] is True

    def fake_show(_device, _command, timeout=None):
        return {"success": True, "output": "hostname R3\n", "error": ""}

    monkeypatch.setattr("app.api.devices.run_show_command", fake_show)
    diff_resp = client.post(
        f"/api/devices/{device_id}/diff",
        json={"template": "vlan_creation", "variables": {"vlan_id": 10, "vlan_name": "OPS"}},
    )
    assert diff_resp.status_code == 200
    assert diff_resp.get_json()["success"] is True

    snapshots_resp = client.get(f"/api/devices/{device_id}/snapshots")
    assert snapshots_resp.status_code == 200
    assert any(item["id"] == snapshot_id for item in snapshots_resp.get_json())

    snapshot_detail_resp = client.get(f"/api/snapshots/{snapshot_id}")
    assert snapshot_detail_resp.status_code == 200
    assert snapshot_detail_resp.get_json()["id"] == snapshot_id

    alerts_resp = client.get(f"/api/alerts?device_id={device_id}&limit=10")
    assert alerts_resp.status_code == 200
    assert len(alerts_resp.get_json()) >= 1

    apply_resp = client.post(
        f"/api/devices/{device_id}/apply_template",
        json={"template": "vlan_creation", "variables": {"vlan_id": 10, "vlan_name": "OPS"}},
    )
    assert apply_resp.status_code == 403
