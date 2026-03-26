import platform
import subprocess
from datetime import datetime, timezone
from typing import Any

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from ..db import db
from ..models.alert import Alert
from ..models.device import Device


scheduler = BackgroundScheduler()


def _ping(ip: str, timeout_ms: int = 800) -> bool:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
    else:
        timeout_seconds = max(1, int(round(timeout_ms / 1000)))
        cmd = ["ping", "-c", "1", "-W", str(timeout_seconds), ip]

    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return result.returncode == 0
    except Exception:
        return False


def _safe_float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return round(float(value), 2)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _host_status_label(is_up: bool) -> str:
    return "Online" if is_up else "Offline"


def _update_inventory_status(devices: list[Device], status_by_id: dict[int, bool]) -> None:
    """
    Update device inventory status and track last successful poll.
    
    Academic: Logs all significant state changes and updates last_seen
    timestamp to track monitoring pipeline health.
    """
    changed = False
    current_utc_time = datetime.now(timezone.utc).replace(tzinfo=None)  # Store as naive UTC
    
    for device in devices:
        new_value = bool(status_by_id.get(device.id, False))
        if bool(device.is_up) != new_value:
            changed = True
            if bool(device.is_up) and not new_value:
                # Device went DOWN
                current_app.logger.critical(
                    f"[MONITOR] Device OFFLINE: {device.name} ({device.ip_address})"
                )
                db.session.add(
                    Alert(severity="crit", message=f"Device DOWN: {device.name} ({device.ip_address})", device_id=device.id)
                )
            elif (not bool(device.is_up)) and new_value:
                # Device came UP
                current_app.logger.info(
                    f"[MONITOR] Device ONLINE: {device.name} ({device.ip_address})"
                )
                db.session.add(
                    Alert(severity="info", message=f"Device UP: {device.name} ({device.ip_address})", device_id=device.id)
                )
            device.is_up = new_value
        
        # Academic: Update last_seen timestamp whenever device responds (is reachable)
        if new_value:
            device.last_seen = current_utc_time
            current_app.logger.debug(
                f"[MONITOR] Device polled: {device.name} - last_seen updated"
            )

    if changed:
        db.session.commit()
        current_app.logger.info(f"[MONITOR] Device status changes committed to database - {len(devices)} devices scanned")


def _zabbix_enabled(config: dict[str, Any]) -> bool:
    """
    Check if Zabbix 7.0 Bearer Token authentication is configured.
    
    Zabbix 7.0 requires:
    - ZABBIX_URL: Base URL to Zabbix instance (e.g., http://192.168.64.135/zabbix)
    - ZABBIX_API_TOKEN: Bearer token for authentication
    """
    return bool(config.get("ZABBIX_URL") and config.get("ZABBIX_API_TOKEN"))


def _pick_item_value(items: list[dict], key_patterns: tuple[str, ...]) -> Any:
    for pattern in key_patterns:
        for item in items:
            key_value = (item.get("key_") or "").lower()
            name_value = (item.get("name") or "").lower()
            if pattern in key_value or pattern in name_value:
                return item.get("lastvalue")
    return None


def _fetch_from_zabbix_bearer(devices: list[Device], config: dict[str, Any]) -> dict:
    """
    Fetch monitoring data from Zabbix 7.0 using Bearer Token authentication.
    
    Zabbix 7.0 uses JSON-RPC API with Bearer Token instead of username/password.
    This implementation:
    1. Authenticates using Bearer Token in Authorization header
    2. Fetches hosts that match configured devices by IP or hostname
    3. Uses problem.get method to count active alerts
    4. Collects performance metrics (CPU, memory, uptime)
    """
    try:
        # Construct Zabbix API URL
        base_url = config.get("ZABBIX_URL", "").rstrip("/")
        api_url = f"{base_url}/api_jsonrpc.php"
        api_token = config.get("ZABBIX_API_TOKEN", "")
        
        headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        }
        
        # Request ID counter for JSON-RPC
        request_id = 1
        
        # Academic: Log authentication attempt
        current_app.logger.debug(
            f"[MONITOR] Zabbix: Attempting Bearer Token auth to {api_url}"
        )
        
        # Get all hosts from Zabbix
        host_payload = {
            "jsonrpc": "2.0",
            "method": "host.get",
            "params": {
                "output": ["hostid", "host", "name", "status", "available"],
                "selectInterfaces": ["ip"],
            },
            "id": request_id,
        }
        request_id += 1
        
        host_response = requests.post(api_url, json=host_payload, headers=headers, timeout=10)
        host_response.raise_for_status()
        host_data = host_response.json()
        
        if host_data.get("error"):
            raise Exception(f"Zabbix API error: {host_data.get('error')}")
        
        hosts = host_data.get("result", [])
        current_app.logger.debug(
            f"[MONITOR] Zabbix: Retrieved {len(hosts)} hosts from API"
        )
        
        # Build host map for matching devices
        host_map: dict[str, dict] = {}
        for host in hosts:
            identifiers = {(host.get("host") or "").strip().lower(), (host.get("name") or "").strip().lower()}
            for iface in host.get("interfaces") or []:
                identifiers.add((iface.get("ip") or "").strip().lower())
            for key in identifiers:
                if key:
                    host_map[key] = host
        
        # Match our devices to Zabbix hosts
        matched_hosts = []
        host_ids = []
        for device in devices:
            match = host_map.get((device.ip_address or "").strip().lower()) or host_map.get((device.name or "").strip().lower())
            if match:
                matched_hosts.append((device, match))
                host_ids.append(match["hostid"])
        
        # Get items (performance metrics) for matched hosts
        items_by_host: dict[str, list[dict]] = {}
        if host_ids:
            items_payload = {
                "jsonrpc": "2.0",
                "method": "item.get",
                "params": {
                    "hostids": list(sorted(set(host_ids))),
                    "output": ["hostid", "name", "key_", "lastvalue"],
                    "filter": {"status": 0},
                },
                "id": request_id,
            }
            request_id += 1
            
            items_response = requests.post(api_url, json=items_payload, headers=headers, timeout=10)
            items_response.raise_for_status()
            items_data = items_response.json()
            
            if items_data.get("error"):
                current_app.logger.warning(
                    f"[MONITOR] Zabbix: Item fetch error: {items_data.get('error')}"
                )
            else:
                items = items_data.get("result", [])
                for item in items:
                    items_by_host.setdefault(item.get("hostid"), []).append(item)
        
        # Get active problems/alerts using problem.get method
        problems_payload = {
            "jsonrpc": "2.0",
            "method": "problem.get",
            "params": {
                "output": ["eventid", "objectid", "source", "object", "eventtype", "severity", "name"],
                "selectHosts": ["host", "name"],
                "filter": {"source": 0, "object": 0},  # Source 0 = trigger, Object 0 = trigger
                "recent": False,
            },
            "id": request_id,
        }
        request_id += 1
        
        problems_response = requests.post(api_url, json=problems_payload, headers=headers, timeout=10)
        problems_response.raise_for_status()
        problems_data = problems_response.json()
        
        if problems_data.get("error"):
            current_app.logger.warning(
                f"[MONITOR] Zabbix: Problem fetch error: {problems_data.get('error')}"
            )
            active_problems = []
        else:
            active_problems = problems_data.get("result", [])
        
        current_app.logger.debug(
            f"[MONITOR] Zabbix: Retrieved {len(active_problems)} active problems"
        )
        
        # Build response payload
        status_by_id: dict[int, bool] = {}
        hosts_payload = []
        for device, host in matched_hosts:
            host_items = items_by_host.get(host.get("hostid"), [])
            is_up = str(host.get("available", "0")) == "1"
            status_by_id[device.id] = is_up
            hosts_payload.append(
                {
                    "device_id": device.id,
                    "device_name": device.name,
                    "ip_address": device.ip_address,
                    "source": "zabbix",
                    "status": _host_status_label(is_up),
                    "cpu": _safe_float(_pick_item_value(host_items, ("system.cpu.util", "system.cpu.load", "cpu"))),
                    "memory": _safe_float(_pick_item_value(host_items, ("vm.memory.size[pused]", "memory.util", "memory"))),
                    "uptime": _safe_int(_pick_item_value(host_items, ("system.uptime", "uptime"))),
                    "host_name": host.get("name") or host.get("host"),
                }
            )
        
        # Format problems into alerts
        problems_payload = [
            {
                "problem_id": problem.get("eventid"),
                "name": problem.get("name"),
                "severity": problem.get("severity"),
                "hosts": [h.get("name") or h.get("host") for h in (problem.get("hosts") or [])],
            }
            for problem in active_problems
        ]
        
        current_app.logger.info(
            f"[MONITOR] Zabbix: Successfully fetched {len(matched_hosts)} hosts, "
            f"{len(active_problems)} active problems"
        )
        
        return {
            "source": "zabbix",
            "status_by_id": status_by_id,
            "hosts": hosts_payload,
            "active_alerts": problems_payload,
        }
    
    except requests.exceptions.Timeout:
        error_msg = "Zabbix API request timeout (10s)"
        current_app.logger.warning(f"[MONITOR] {error_msg}")
        raise Exception(error_msg)
    except requests.exceptions.ConnectionError as e:
        error_msg = f"Zabbix connection failed: {e}"
        current_app.logger.warning(f"[MONITOR] {error_msg}")
        raise Exception(error_msg)
    except Exception as e:
        error_msg = f"Zabbix API error: {e}"
        current_app.logger.warning(f"[MONITOR] {error_msg}")
        raise Exception(error_msg)


def _build_ping_host_payload(device: Device, timeout_ms: int) -> dict:
    is_up = _ping(device.ip_address, timeout_ms=timeout_ms)
    return {
        "device_id": device.id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "source": "ping",
        "status": _host_status_label(is_up),
        "cpu": None,
        "memory": None,
        "uptime": None,
        "host_name": device.name,
        "is_up": is_up,
    }


def _fetch_with_ping_fallback(devices: list[Device], config: dict[str, Any], error_message: str | None = None) -> dict:
    timeout_ms = int(config.get("MONITOR_PING_TIMEOUT_MS", 800))
    status_by_id: dict[int, bool] = {}
    hosts_payload = []
    for device in devices:
        host_payload = _build_ping_host_payload(device, timeout_ms)
        status_by_id[device.id] = host_payload.pop("is_up")
        hosts_payload.append(host_payload)

    return {
        "source": "ping",
        "status_by_id": status_by_id,
        "hosts": hosts_payload,
        "active_alerts": [],
        "warning": error_message,
    }


def fetch_monitoring_snapshot(devices: list[Device] | None = None, update_inventory: bool = True) -> dict:
    """
    Fetch monitoring snapshot from Zabbix 7.0 or fallback to ping.
    
    Academic: Demonstrates graceful degradation - if Zabbix is unavailable,
    automatically falls back to ICMP ping for basic connectivity detection.
    """
    config = dict(current_app.config)
    devices = devices or Device.query.order_by(Device.name.asc()).all()
    timeout_ms = int(config.get("MONITOR_PING_TIMEOUT_MS", 800))

    try:
        if _zabbix_enabled(config):
            payload = _fetch_from_zabbix_bearer(devices, config)
            hosts_by_device_id = {item.get("device_id"): item for item in payload.get("hosts", [])}
            status_by_id = dict(payload.get("status_by_id", {}))

            # Fill in devices not monitored by Zabbix with ping fallback
            for device in devices:
                if device.id in status_by_id:
                    continue

                host_payload = _build_ping_host_payload(device, timeout_ms)
                status_by_id[device.id] = host_payload.pop("is_up")
                hosts_by_device_id[device.id] = host_payload

            ordered_hosts = []
            for device in devices:
                host_payload = hosts_by_device_id.get(device.id)
                if host_payload:
                    ordered_hosts.append(host_payload)

            payload["hosts"] = ordered_hosts
            payload["status_by_id"] = status_by_id
        else:
            payload = _fetch_with_ping_fallback(devices, config, error_message="Zabbix 7.0 not configured (ZABBIX_URL or ZABBIX_API_TOKEN missing).")
    except Exception as exc:
        error_detail = str(exc)
        current_app.logger.warning(
            f"[MONITOR] Zabbix fetch failed, falling back to ping: {error_detail}"
        )
        payload = _fetch_with_ping_fallback(devices, config, error_message=f"Zabbix unavailable: {error_detail}")

    if update_inventory:
        _update_inventory_status(devices, payload.get("status_by_id", {}))

    payload["generated_at"] = datetime.utcnow().isoformat()
    payload["summary"] = {
        "total_devices": len(devices),
        "devices_online": sum(1 for value in payload.get("status_by_id", {}).values() if value),
        "active_zabbix_alerts": len(payload.get("active_alerts", [])),
    }
    return payload


def poll_devices() -> None:
    devices = Device.query.order_by(Device.id.asc()).all()
    fetch_monitoring_snapshot(devices=devices, update_inventory=True)


def start_monitor(app) -> None:
    if scheduler.running:
        return

    with app.app_context():
        interval = app.config.get("MONITOR_INTERVAL_SECONDS", 10)

    def job_wrapper():
        with app.app_context():
            poll_devices()

    scheduler.add_job(job_wrapper, "interval", seconds=interval, id="device_monitor", replace_existing=True)
    scheduler.start()
