import platform
import random
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

try:
    from netmiko import ConnectHandler  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    ConnectHandler = None

from ..db import db
from ..models.alert import Alert
from ..models.device import Device
from ..models.metrics import Metrics


scheduler = BackgroundScheduler()


def _ping(ip: str, timeout_ms: int = 800) -> bool:
    """Perform ICMP ping to device."""
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
    """Safely convert value to float."""
    try:
        if value in (None, ""):
            return None
        return round(float(value), 2)
    except Exception:
        return None


def _safe_int(value: Any) -> int | None:
    """Safely convert value to integer."""
    try:
        if value in (None, ""):
            return None
        return int(float(value))
    except Exception:
        return None


def _host_status_label(is_up: bool) -> str:
    """Return status label based on connectivity."""
    return "Online" if is_up else "Offline"


def _get_device_credentials(device: Device | None = None) -> tuple[str | None, str | None, str | None]:
    """
    Get SSH credentials from app config or device-specific config.
    
    Priority:
    1. Device-specific enable_secret (if available)
    2. Global DEVICE_ENABLE_SECRET from config
    """
    config = dict(current_app.config)
    username = config.get("DEVICE_SSH_USER")
    password = config.get("DEVICE_SSH_PASS")
    
    # Prefer device-specific enable_secret
    secret = None
    if device and hasattr(device, 'enable_secret') and device.enable_secret:
        secret = device.enable_secret
    else:
        secret = config.get("DEVICE_ENABLE_SECRET")
    
    return username, password, secret


def _netmiko_device_type(vendor: str | None, device_type: str | None) -> str | None:
    """Map vendor and device type to Netmiko device type."""
    vendor_name = (vendor or "").strip().lower()
    device_kind = (device_type or "").strip().lower()

    if vendor_name == "cisco":
        if device_kind in {"router", "switch"}:
            return "cisco_ios"
        if device_kind in {"nxos", "nexus"}:
            return "cisco_nxos"
    if vendor_name == "arista":
        return "arista_eos"
    if vendor_name == "juniper":
        return "juniper_junos"
    if vendor_name in {"linux", "generic"} or device_kind == "linux":
        return "linux"
    return None


def _parse_cisco_cpu(output: str) -> float | None:
    """Parse CPU usage from Cisco 'show processes cpu' output using regex."""
    try:
        # Pattern for five second average CPU usage
        patterns = [
            r"CPU\s+utilization.*?:\s+(\d+)%",  # Generic CPU utilization pattern
            r"five\s+second.*?:\s+(\d+)%",      # Five second average
            r"one\s+minute.*?:\s+(\d+)%",       # One minute average
        ]
        
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE)
            if match:
                return _safe_float(match.group(1))
        return None
    except Exception:
        return None


def _parse_cisco_memory(output: str) -> float | None:
    """Parse memory usage from Cisco 'show memory' output using regex."""
    try:
        # Look for memory utilization percentages
        patterns = [
            r"Processor.*?(\d+)\s*%",  # Processor memory percentage
            r"Head.*?(\d+)\s*%",       # Head memory percentage
            r"memory.*usage.*?(\d+)%", # Generic memory usage
        ]
        
        for pattern in patterns:
            match = re.search(pattern, output, re.IGNORECASE | re.DOTALL)
            if match:
                return _safe_float(match.group(1))
        return None
    except Exception:
        return None


def _parse_uptime(output: str) -> int | None:
    """Parse uptime in seconds from device output."""
    try:
        # Pattern: "Device uptime is X.Y hours" or similar
        patterns = [
            r"uptime\s+is\s+(\d+)\s+day[s]?\s+(\d+)\s+hour[s]?\s+(\d+)\s+minute[s]?",  # Days, hours, minutes
            r"uptime\s+is\s+(\d+)\s+hour[s]?\s+(\d+)\s+minute[s]?",  # Hours and minutes
            r"uptime is\s+(\d+)\s+minute[s]?",  # Minutes only
        ]
        
        match = re.search(patterns[0], output, re.IGNORECASE)
        if match:
            days, hours, minutes = int(match.group(1)), int(match.group(2)), int(match.group(3))
            return (days * 86400) + (hours * 3600) + (minutes * 60)
        
        match = re.search(patterns[1], output, re.IGNORECASE)
        if match:
            hours, minutes = int(match.group(1)), int(match.group(2))
            return (hours * 3600) + (minutes * 60)
        
        match = re.search(patterns[2], output, re.IGNORECASE)
        if match:
            minutes = int(match.group(1))
            return minutes * 60
        
        return None
    except Exception:
        return None


def _fetch_metrics_via_ssh(device: Device) -> dict[str, Any]:
    """
    Connect to device via SSH using Netmiko and fetch CPU/Memory metrics.
    
    Establishes SSH connection using Netmiko with device-specific or global
    enable secret, parses CPU/Memory using robust regex patterns, and returns
    metrics dict.
    
    Returns dict with:
    - device_id: int
    - success: bool (True if SSH connection and metric extraction succeeded)
    - is_up: bool (True if device responded via SSH)
    - degraded: bool (True if SSH failed but should trigger ping fallback)
    - cpu: float or None (CPU percentage)
    - memory: float or None (Memory percentage)
    - uptime: int or None (uptime in seconds)
    - error: str (error message if failed)
    """
    if ConnectHandler is None:
        return {
            "device_id": device.id,
            "success": False,
            "is_up": False,
            "degraded": False,
            "cpu": None,
            "memory": None,
            "uptime": None,
            "error": "Netmiko not installed",
        }

    username, password, secret = _get_device_credentials(device=device)
    if not username or not password or not secret:
        return {
            "device_id": device.id,
            "success": False,
            "is_up": False,
            "degraded": False,
            "cpu": None,
            "memory": None,
            "uptime": None,
            "error": "Missing SSH credentials in config",
        }

    device_type = _netmiko_device_type(device.vendor, device.device_type)
    if not device_type:
        return {
            "device_id": device.id,
            "success": False,
            "is_up": False,
            "degraded": False,
            "cpu": None,
            "memory": None,
            "uptime": None,
            "error": f"Unsupported vendor/device type: {device.vendor}/{device.device_type}",
        }

    timeout = int(current_app.config.get("MONITOR_SSH_TIMEOUT_SECONDS", 15))
    conn = None
    try:
        # Enterprise: Add network jitter to prevent SYN flooding when ThreadPoolExecutor spins up
        time.sleep(random.uniform(0.1, 0.5))
        
        current_app.logger.debug(f"[MONITOR] Connecting to {device.name} ({device.ip_address})")
        
        conn = ConnectHandler(
            device_type=device_type,
            host=device.ip_address,
            port=device.ssh_port or 22,
            username=username,
            password=password,
            secret=secret,
            conn_timeout=timeout,
            auth_timeout=timeout,
            banner_timeout=timeout,
            timeout=timeout,
            fast_cli=False,
        )
        
        # Enable privilege mode
        conn.enable()
        
        # Fetch CPU usage
        cpu_output = conn.send_command("show processes cpu", read_timeout=timeout)
        cpu = _parse_cisco_cpu(cpu_output)
        
        # Fetch memory usage
        memory_output = conn.send_command("show memory", read_timeout=timeout)
        memory = _parse_cisco_memory(memory_output)
        
        # Fetch uptime
        uptime_output = conn.send_command("show version", read_timeout=timeout)
        uptime = _parse_uptime(uptime_output)
        
        current_app.logger.info(
            f"[MONITOR] Successfully polled {device.name}: CPU={cpu}%, Memory={memory}%, Uptime={uptime}s"
        )
        
        return {
            "device_id": device.id,
            "success": True,
            "is_up": True,
            "degraded": False,
            "cpu": cpu,
            "memory": memory,
            "uptime": uptime,
            "error": "",
        }
    
    except Exception as exc:
        current_app.logger.warning(f"[MONITOR] Failed to poll {device.name}: {exc}")
        return {
            "device_id": device.id,
            "success": False,
            "is_up": False,
            "degraded": True,  # Will trigger ping fallback to determine if truly offline
            "cpu": None,
            "memory": None,
            "uptime": None,
            "error": str(exc),
        }
    
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def _save_metrics_to_db(device_id: int, metrics: dict[str, Any]) -> None:
    """Save metrics to the Metrics model."""
    if not metrics.get("success"):
        return
    
    timestamp = datetime.now(timezone.utc).replace(tzinfo=None)
    metric_data = [
        ("cpu_usage", metrics.get("cpu")),
        ("memory_usage", metrics.get("memory")),
        ("uptime_ticks", metrics.get("uptime")),
    ]
    
    for metric_name, value in metric_data:
        if value is not None:
            m = Metrics(
                device_id=device_id,
                metric_name=metric_name,
                value=value,
                timestamp=timestamp,
            )
            db.session.add(m)
    
    db.session.commit()


def _update_inventory_status(devices: list[Device], status_by_id: dict[int, bool], degraded_by_id: dict[int, bool] | None = None) -> None:
    """
    Update device inventory status and track last successful poll.
    
    Args:
        devices: List of Device models
        status_by_id: Map of device_id -> is_up (bool)
        degraded_by_id: Map of device_id -> is_degraded (bool). If None, all False.
    """
    degraded_by_id = degraded_by_id or {}
    changed = False
    current_utc_time = datetime.now(timezone.utc).replace(tzinfo=None)
    
    for device in devices:
        new_value = bool(status_by_id.get(device.id, False))
        new_degraded = bool(degraded_by_id.get(device.id, False))
        
        # Check for status changes
        status_changed = bool(device.is_up) != new_value
        degraded_changed = bool(device.degraded_status) != new_degraded
        
        if status_changed or degraded_changed:
            changed = True
            
            if bool(device.is_up) and not new_value:
                # Device went DOWN
                current_app.logger.critical(
                    f"[MONITOR] Device OFFLINE: {device.name} ({device.ip_address})"
                )
                db.session.add(
                    Alert(
                        severity="crit",
                        message=f"Device DOWN: {device.name} ({device.ip_address})",
                        device_id=device.id,
                    )
                )
            elif (not bool(device.is_up)) and new_value:
                # Device came UP (or recovered from degraded)
                if new_degraded:
                    current_app.logger.warning(
                        f"[MONITOR] Device DEGRADED (SSH failed, ICMP OK): {device.name} ({device.ip_address})"
                    )
                    db.session.add(
                        Alert(
                            severity="warn",
                            message=f"Device DEGRADED: {device.name} ({device.ip_address}) - SSH unavailable, ICMP reachable",
                            device_id=device.id,
                        )
                    )
                else:
                    current_app.logger.info(
                        f"[MONITOR] Device ONLINE: {device.name} ({device.ip_address})"
                    )
                    db.session.add(
                        Alert(
                            severity="info",
                            message=f"Device UP: {device.name} ({device.ip_address})",
                            device_id=device.id,
                        )
                    )
            elif new_value and degraded_changed:
                # Device status changed between degraded and online
                if new_degraded and not device.degraded_status:
                    current_app.logger.warning(
                        f"[MONITOR] Device entered DEGRADED state: {device.name} ({device.ip_address})"
                    )
                    db.session.add(
                        Alert(
                            severity="warn",
                            message=f"Device DEGRADED: {device.name} ({device.ip_address}) - SSH unavailable, ICMP reachable",
                            device_id=device.id,
                        )
                    )
                elif not new_degraded and device.degraded_status:
                    current_app.logger.info(
                        f"[MONITOR] Device recovered to ONLINE: {device.name} ({device.ip_address})"
                    )
                    db.session.add(
                        Alert(
                            severity="info",
                            message=f"Device RECOVERED: {device.name} ({device.ip_address}) - SSH restored",
                            device_id=device.id,
                        )
                    )
            
            device.is_up = new_value
            device.degraded_status = new_degraded
        
        # Update last_seen timestamp whenever device is reachable (online or degraded)
        if new_value:
            device.last_seen = current_utc_time
            current_app.logger.debug(f"[MONITOR] Device polled: {device.name} - last_seen updated")
    
    if changed:
        db.session.commit()
        current_app.logger.info(
            f"[MONITOR] Device status changes committed - {len(devices)} devices scanned"
        )


def _build_netmiko_host_payload(device: Device, metrics: dict[str, Any]) -> dict:
    """Build host payload with Netmiko-fetched metrics."""
    return {
        "device_id": device.id,
        "device_name": device.name,
        "ip_address": device.ip_address,
        "source": "netmiko",
        "status": _host_status_label(metrics.get("is_up", False)),
        "cpu": metrics.get("cpu"),
        "memory": metrics.get("memory"),
        "uptime": metrics.get("uptime"),
        "host_name": device.name,
    }


def _build_ping_host_payload(device: Device, timeout_ms: int) -> dict:
    """Build host payload using simple ping."""
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


def _fetch_with_ping_fallback(devices: list[Device], config: dict[str, Any]) -> dict:
    """Fallback to ICMP ping when SSH monitoring is unavailable."""
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
    }


def fetch_monitoring_snapshot(devices: list[Device] | None = None, update_inventory: bool = True) -> dict:
    """
    Fetch monitoring snapshot using Netmiko SSH or fallback to ping.
    
    Tries SSH first on all devices concurrently via ThreadPoolExecutor.
    For devices where SSH fails, falls back to ICMP ping to determine if truly offline
    or just SSH-unavailable (degraded status).
    
    Returns monitoring payload with Online/Degraded/Offline status for each device.
    """
    config = dict(current_app.config)
    devices = devices or Device.query.order_by(Device.name.asc()).all()
    timeout_ms = int(config.get("MONITOR_PING_TIMEOUT_MS", 800))
    
    status_by_id: dict[int, bool] = {}
    degraded_by_id: dict[int, bool] = {}
    hosts_payload = []
    max_workers = int(config.get("MONITOR_MAX_WORKERS", 5))
    
    try:
        # Parallel SSH monitoring using Netmiko
        metrics_results: dict[int, dict[str, Any]] = {}
        
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="monitor-ssh") as executor:
            future_map = {
                executor.submit(_fetch_metrics_via_ssh, device): device.id
                for device in devices
            }
            
            for future in as_completed(future_map):
                device_id = future_map[future]
                try:
                    metrics = future.result()
                    metrics_results[device_id] = metrics
                except Exception as exc:
                    current_app.logger.warning(f"[MONITOR] SSH polling error for device {device_id}: {exc}")
                    metrics_results[device_id] = {
                        "device_id": device_id,
                        "success": False,
                        "is_up": False,
                        "degraded": True,
                        "error": str(exc),
                    }
        
        # Build response and save metrics
        for device in devices:
            metrics = metrics_results.get(device.id, {"success": False, "is_up": False, "degraded": True})
            
            # If SSH was successful, device is online (not degraded)
            if metrics.get("success"):
                status_by_id[device.id] = True
                degraded_by_id[device.id] = False
                _save_metrics_to_db(device.id, metrics)
                hosts_payload.append(_build_netmiko_host_payload(device, metrics))
            else:
                # SSH failed - try ping to see if device is truly offline or degraded
                ping_payload = _build_ping_host_payload(device, timeout_ms)
                is_ping_up = ping_payload.pop("is_up")
                status_by_id[device.id] = is_ping_up
                
                # Degraded = SSH failed but ping succeeded
                if is_ping_up:
                    degraded_by_id[device.id] = True
                    current_app.logger.warning(
                        f"[MONITOR] Device {device.name} is DEGRADED (SSH failed, ping OK)"
                    )
                else:
                    degraded_by_id[device.id] = False
                    current_app.logger.critical(
                        f"[MONITOR] Device {device.name} is OFFLINE (SSH and ping both failed)"
                    )
                
                hosts_payload.append(ping_payload)
        
        payload = {
            "source": "netmiko",
            "status_by_id": status_by_id,
            "hosts": hosts_payload,
        }
    
    except Exception as exc:
        current_app.logger.warning(f"[MONITOR] Netmiko monitoring failed, falling back to ping: {exc}")
        payload = _fetch_with_ping_fallback(devices, config)
        # Reset degraded status when falling back to pure ping
        degraded_by_id = {}
        status_by_id = payload.get("status_by_id", {})
    
    if update_inventory:
        _update_inventory_status(devices, status_by_id, degraded_by_id)
    
    payload["generated_at"] = datetime.utcnow().isoformat()
    payload["summary"] = {
        "total_devices": len(devices),
        "devices_online": sum(1 for value in payload.get("status_by_id", {}).values() if value),
        "devices_degraded": sum(1 for value in degraded_by_id.values() if value),
    }
    return payload


def poll_devices() -> None:
    """Poll all devices for monitoring data."""
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
