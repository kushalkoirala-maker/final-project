import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from flask import current_app

try:
    from netmiko import ConnectHandler  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    ConnectHandler = None


DEFAULT_TIMEOUT_SECONDS = 15


def _result(success: bool, output: str = "", error: str = "", **extra: Any) -> dict:
    payload = {"success": success, "output": output, "error": error}
    payload.update(extra)
    return payload


def _normalize_vendor(vendor: str | None) -> str:
    return (vendor or "").strip().lower()


def _normalize_device_type(device_type: str | None) -> str:
    return (device_type or "").strip().lower()


def netmiko_device_type(vendor: str | None, device_type: str | None) -> str | None:
    vendor_name = _normalize_vendor(vendor)
    device_kind = _normalize_device_type(device_type)

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


def _current_config() -> dict[str, Any]:
    try:
        return dict(current_app.config)
    except Exception:
        return {}


def get_connection_credentials(config: dict[str, Any] | None = None) -> tuple[str | None, str | None, str | None]:
    cfg = config or _current_config()
    username = os.getenv("DEVICE_SSH_USER") or cfg.get("DEVICE_SSH_USER")
    password = os.getenv("DEVICE_SSH_PASS") or cfg.get("DEVICE_SSH_PASS")
    secret = (
        os.getenv("DEVICE_ENABLE_SECRET")
        or os.getenv("DEVICE_ENABLE_PASS")
        or cfg.get("DEVICE_ENABLE_SECRET")
        or cfg.get("DEVICE_ENABLE_PASS")
    )
    return username, password, secret


def serialize_device(device: Any) -> dict[str, Any]:
    if isinstance(device, dict):
        return {
            "id": device.get("id"),
            "name": device.get("name"),
            "ip_address": device.get("ip_address"),
            "vendor": device.get("vendor"),
            "device_type": device.get("device_type"),
            "ssh_port": device.get("ssh_port", 22),
        }

    return {
        "id": getattr(device, "id", None),
        "name": getattr(device, "name", None),
        "ip_address": getattr(device, "ip_address", None),
        "vendor": getattr(device, "vendor", None),
        "device_type": getattr(device, "device_type", None),
        "ssh_port": getattr(device, "ssh_port", 22),
    }


def _connect_params(device: dict[str, Any], timeout: int, config: dict[str, Any] | None = None) -> dict[str, Any]:
    username, password, secret = get_connection_credentials(config=config)
    if not username or not password:
        raise ValueError("Missing DEVICE_SSH_USER or DEVICE_SSH_PASS in environment/config")
    if not secret:
        raise ValueError("Missing DEVICE_ENABLE_SECRET in environment/config")

    device_type = netmiko_device_type(device.get("vendor"), device.get("device_type"))
    if not device_type:
        raise ValueError(
            f"Unsupported vendor/device type combination: {device.get('vendor')} / {device.get('device_type')}"
        )

    # Enterprise: Use device-specific SSH port (defaults to 22)
    ssh_port = device.get("ssh_port", 22)

    return {
        "device_type": device_type,
        "host": device.get("ip_address"),
        "port": ssh_port,
        "username": username,
        "password": password,
        "secret": secret,
        "conn_timeout": timeout,
        "auth_timeout": timeout,
        "banner_timeout": timeout,
        "timeout": timeout,
        "fast_cli": False,
    }


def _connect_and_enable(device_payload: dict[str, Any], timeout: int, config: dict[str, Any] | None = None):
    params = _connect_params(device_payload, timeout, config=config)
    conn = ConnectHandler(**params)
    conn.secret = params["secret"]
    conn.enable()
    return conn


def run_show_command(
    device: Any,
    command: str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    config: dict[str, Any] | None = None,
) -> dict:
    command = (command or "").strip()
    if not command:
        return _result(False, error="Command is required")
    if ConnectHandler is None:
        return _result(False, error="Netmiko is not installed")

    device_payload = serialize_device(device)
    conn = None
    try:
        conn = _connect_and_enable(device_payload, timeout, config=config)
        prompt = conn.find_prompt()
        output = conn.send_command(command, read_timeout=timeout)
        return _result(
            True,
            output=output,
            prompt=prompt,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except Exception as exc:
        return _result(
            False,
            error=f"Netmiko error: {exc}",
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def _save_running_config(conn: Any, timeout: int) -> str:
    try:
        return conn.save_config()
    except Exception:
        return conn.send_command("write memory", read_timeout=timeout)


def push_config_commands(
    device: Any,
    commands: list[str] | str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    config: dict[str, Any] | None = None,
) -> dict:
    if isinstance(commands, str):
        commands = [commands]
    commands = [cmd.strip() for cmd in (commands or []) if cmd and cmd.strip()]
    if not commands:
        return _result(False, error="At least one config command is required")
    if ConnectHandler is None:
        return _result(False, error="Netmiko is not installed")

    device_payload = serialize_device(device)
    conn = None
    try:
        conn = _connect_and_enable(device_payload, timeout, config=config)
        
        # Academic: Explicitly verify privilege level before config push
        prompt = conn.find_prompt()
        current_app.logger.info(
            f"[AUTOMATION] Connected to device {device_payload.get('name')} ({device_payload.get('ip_address')}) - Privilege prompt: {prompt}"
        )
        
        # Ensure we are in enable/privileged mode before sending config commands
        if not conn.check_enable_mode():
            current_app.logger.warning(
                f"[AUTOMATION] Device {device_payload.get('name')} not in enable mode, attempting enable again"
            )
            conn.enable()
        
        config_output = conn.send_config_set(commands, read_timeout=timeout)
        save_output = _save_running_config(conn, timeout)
        output = "\n".join(part for part in [config_output, save_output] if part)
        
        current_app.logger.info(
            f"[AUTOMATION] Configuration successfully pushed to device {device_payload.get('name')} - Commands: {len(commands)}"
        )
        
        return _result(
            True,
            output=output,
            prompt=prompt,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
            commands=commands,
            save_output=save_output,
        )
    except Exception as exc:
        error_msg = f"Netmiko error: {exc}"
        current_app.logger.error(
            f"[AUTOMATION] Configuration push failed for device {device_payload.get('name')}: {error_msg}"
        )
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
            commands=commands,
        )
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def test_connection(device: Any, timeout: int = 8, config: dict[str, Any] | None = None) -> dict:
    if ConnectHandler is None:
        return _result(False, error="Netmiko is not installed")

    device_payload = serialize_device(device)
    conn = None
    try:
        conn = _connect_and_enable(device_payload, timeout, config=config)
        prompt = conn.find_prompt()
        output = "\n".join(
            [
                "Connection established successfully.",
                f"Privilege prompt: {prompt}",
            ]
        )
        return _result(
            True,
            output=output,
            prompt=prompt,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except Exception as exc:
        return _result(
            False,
            error=f"Netmiko connection test failed: {exc}",
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    finally:
        if conn is not None:
            try:
                conn.disconnect()
            except Exception:
                pass


def execute_config_job(
    device_list: list[Any],
    commands: list[str] | str,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
    max_workers: int | None = None,
) -> list[dict]:
    if isinstance(commands, str):
        commands = [commands]
    commands = [cmd.strip() for cmd in (commands or []) if cmd and cmd.strip()]
    if not commands:
        return [_result(False, error="At least one config command is required")]

    serialized_devices = [serialize_device(device) for device in (device_list or [])]
    if not serialized_devices:
        return []

    cfg = _current_config()
    worker_limit = max_workers or cfg.get("AUTOMATION_MAX_WORKERS") or 10
    worker_count = max(1, min(int(worker_limit), len(serialized_devices)))
    ordered_results: list[dict | None] = [None] * len(serialized_devices)

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="netmiko-job") as executor:
        future_map = {
            executor.submit(push_config_commands, device, commands, timeout, cfg): index
            for index, device in enumerate(serialized_devices)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                ordered_results[index] = future.result()
            except Exception as exc:
                device = serialized_devices[index]
                ordered_results[index] = _result(
                    False,
                    error=f"Unhandled job execution error: {exc}",
                    device_id=device.get("id"),
                    device_name=device.get("name"),
                    commands=commands,
                )

    return [item for item in ordered_results if item is not None]
