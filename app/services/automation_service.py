import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from flask import current_app

try:
    from netmiko import ConnectHandler, NetmikoTimeoutException, NetmikoAuthenticationException  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    ConnectHandler = None
    NetmikoTimeoutException = None
    NetmikoAuthenticationException = None


DEFAULT_TIMEOUT_SECONDS = 15


def _result(success: bool, output: str = "", error: str = "", **extra: Any) -> dict:
    """Build standardized result payload."""
    payload = {"success": success, "output": output, "error": error}
    payload.update(extra)
    return payload


def _normalize_vendor(vendor: str | None) -> str:
    """Normalize vendor name to lowercase."""
    return (vendor or "").strip().lower()


def _normalize_device_type(device_type: str | None) -> str:
    """Normalize device type to lowercase."""
    return (device_type or "").strip().lower()


def netmiko_device_type(vendor: str | None, device_type: str | None) -> str | None:
    """
    Map vendor and device_type to Netmiko device type string.
    
    Supports:
    - Cisco IOS (router, switch)
    - Cisco NX-OS (nxos, nexus)
    - Arista EOS
    - Juniper JunOS
    - Linux hosts
    """
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
    """Get current Flask app config as dict."""
    try:
        return dict(current_app.config)
    except Exception:
        return {}


def get_connection_credentials(
    device: Any = None, 
    config: dict[str, Any] | None = None
) -> tuple[str | None, str | None, str | None]:
    """
    Get SSH credentials for device connection.
    
    Priority:
    1. Device-specific enable_secret (if device has enable_secret column)
    2. DEVICE_ENABLE_SECRET from environment/config
    3. DEVICE_ENABLE_PASS from environment/config (fallback)
    
    Returns:
        Tuple of (username, password, secret)
    """
    cfg = config or _current_config()
    username = os.getenv("DEVICE_SSH_USER") or cfg.get("DEVICE_SSH_USER")
    password = os.getenv("DEVICE_SSH_PASS") or cfg.get("DEVICE_SSH_PASS")
    
    # Prefer device-specific enable_secret, fall back to global config
    secret = None
    if device and hasattr(device, 'enable_secret') and device.enable_secret:
        secret = device.enable_secret
    else:
        secret = (
            os.getenv("DEVICE_ENABLE_SECRET")
            or os.getenv("DEVICE_ENABLE_PASS")
            or cfg.get("DEVICE_ENABLE_SECRET")
            or cfg.get("DEVICE_ENABLE_PASS")
        )
    
    return username, password, secret


def serialize_device(device: Any) -> dict[str, Any]:
    """
    Serialize device object (ORM model or dict) to connection params dict.
    """
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


def _connect_params(
    device: dict[str, Any], 
    timeout: int, 
    config: dict[str, Any] | None = None,
    device_obj: Any = None
) -> dict[str, Any]:
    """
    Build Netmiko ConnectHandler parameters from device details.
    
    Raises:
        ValueError: If required credentials or device type mapping not found
    """
    username, password, secret = get_connection_credentials(device=device_obj, config=config)
    if not username or not password:
        raise ValueError("Missing DEVICE_SSH_USER or DEVICE_SSH_PASS in environment/config")
    if not secret:
        raise ValueError("Missing DEVICE_ENABLE_SECRET in environment/config")

    device_type = netmiko_device_type(device.get("vendor"), device.get("device_type"))
    if not device_type:
        raise ValueError(
            f"Unsupported vendor/device type combination: {device.get('vendor')} / {device.get('device_type')}"
        )

    # Support non-standard SSH ports
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


def _connect_and_enable(
    device_payload: dict[str, Any], 
    timeout: int, 
    config: dict[str, Any] | None = None,
    device_obj: Any = None
):
    """
    Establish SSH connection and enter privilege mode.
    
    1. Add network jitter (random delay 0.1-0.5s) to prevent SYN flooding
    2. Connect to device
    3. Set secret (enable password)
    4. Call enable() to enter privilege mode
    5. Return connection object
    
    Raises:
        NetmikoTimeoutException: If connection times out
        NetmikoAuthenticationException: If authentication fails
        Exception: Other Netmiko errors
    """
    params = _connect_params(device_payload, timeout, config=config, device_obj=device_obj)
    
    # Enterprise: Add network jitter to prevent SYN flooding when ThreadPoolExecutor spins up
    time.sleep(random.uniform(0.1, 0.5))
    
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
    """
    Execute a read-only show command on a device.
    
    Args:
        device: Device ORM model or dict
        command: Show command to execute
        timeout: SSH timeout in seconds
        config: Flask app config dict (if not using current_app context)
    
    Returns:
        Result dict with success, output, error, prompt, device_id, device_name
    """
    command = (command or "").strip()
    if not command:
        return _result(False, error="Command is required")
    if ConnectHandler is None:
        return _result(False, error="Netmiko is not installed")

    device_payload = serialize_device(device)
    conn = None
    try:
        conn = _connect_and_enable(device_payload, timeout, config=config, device_obj=device)
        prompt = conn.find_prompt()
        output = conn.send_command(command, read_timeout=timeout)
        return _result(
            True,
            output=output,
            prompt=prompt,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except NetmikoTimeoutException as exc:
        error_msg = f"Connection timeout after {timeout}s: {str(exc)}"
        current_app.logger.warning(f"[AUTOMATION] Timeout on {device_payload.get('name')}: {error_msg}")
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except NetmikoAuthenticationException as exc:
        error_msg = f"SSH authentication failed: {str(exc)}"
        current_app.logger.error(f"[AUTOMATION] Auth failure on {device_payload.get('name')}: {error_msg}")
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except Exception as exc:
        error_msg = f"Netmiko error: {str(exc)}"
        current_app.logger.error(f"[AUTOMATION] Command execution failed on {device_payload.get('name')}: {error_msg}")
        return _result(
            False,
            error=error_msg,
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
    """
    Save running config to startup config on device.
    
    Tries save_config() first, falls back to 'write memory' command.
    """
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
    """
    Push configuration commands to device.
    
    Workflow:
    1. Connect and enter privilege mode
    2. Verify privilege mode with check_enable_mode()
    3. Send configuration commands with cmd_verify=True
    4. Save configuration to startup
    5. Log all operations
    
    Error Handling:
    - Catches NetmikoTimeoutException, NetmikoAuthenticationException
    - Returns clean error dicts (no 500 errors thrown)
    
    Key Technical Details:
    - enable() is explicitly called via _connect_and_enable() before config mode
    - cmd_verify=True ensures Netmiko confirms each command via prompt detection
    - find_prompt() is used dynamically to detect hostname changes
    
    Args:
        device: Device ORM model or dict
        commands: Single command string or list of command strings
        timeout: SSH timeout in seconds
        config: Flask app config dict
    
    Returns:
        Result dict with success, output, error, prompt, commands list
    """
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
        conn = _connect_and_enable(device_payload, timeout, config=config, device_obj=device)
        
        # Get current prompt for logging - this is dynamic and resilient to hostname changes
        base_prompt = conn.find_prompt()
        current_app.logger.info(
            f"[AUTOMATION] Connected to {device_payload.get('name')} ({device_payload.get('ip_address')}) - Prompt: {base_prompt}"
        )
        
        # Explicitly verify privilege mode before config push
        if not conn.check_enable_mode():
            current_app.logger.warning(
                f"[AUTOMATION] Device {device_payload.get('name')} not in enable mode, attempting enable again"
            )
            conn.enable()
        
        # Re-verify after second enable attempt
        if not conn.check_enable_mode():
            error_msg = "Failed to enter privilege mode after enable() call"
            current_app.logger.error(f"[AUTOMATION] {error_msg} - {device_payload.get('name')}")
            return _result(
                False,
                error=error_msg,
                device_id=device_payload.get("id"),
                device_name=device_payload.get("name"),
                commands=commands,
            )
        
        # Send config commands with cmd_verify=True for robust prompt detection
        # This prevents buffer desync and handles prompt changes gracefully
        config_output = conn.send_config_set(
            commands,
            enter_config_mode=True,
            exit_config_mode=True,
            cmd_verify=True,
            read_timeout=timeout
        )
        save_output = _save_running_config(conn, timeout)
        output = "\n".join(part for part in [config_output, save_output] if part)
        
        current_app.logger.info(
            f"[AUTOMATION] Configuration successfully pushed to {device_payload.get('name')} - Commands: {len(commands)}"
        )
        
        return _result(
            True,
            output=output,
            prompt=base_prompt,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
            commands=commands,
            save_output=save_output,
        )
    
    except NetmikoTimeoutException as exc:
        error_msg = f"Connection timeout during config push: {str(exc)}"
        current_app.logger.error(f"[AUTOMATION] Timeout on {device_payload.get('name')}: {error_msg}")
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
            commands=commands,
        )
    except NetmikoAuthenticationException as exc:
        error_msg = f"SSH authentication failed during config push: {str(exc)}"
        current_app.logger.error(f"[AUTOMATION] Auth failure on {device_payload.get('name')}: {error_msg}")
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
            commands=commands,
        )
    except Exception as exc:
        error_msg = f"Configuration push failed: {str(exc)}"
        current_app.logger.error(
            f"[AUTOMATION] Config push error on {device_payload.get('name')}: {error_msg}"
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
    """
    Test SSH connection to device.
    
    Returns device prompt if successful, error message otherwise.
    """
    if ConnectHandler is None:
        return _result(False, error="Netmiko is not installed")

    device_payload = serialize_device(device)
    conn = None
    try:
        conn = _connect_and_enable(device_payload, timeout, config=config, device_obj=device)
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
    except NetmikoTimeoutException as exc:
        error_msg = f"Connection timeout: {str(exc)}"
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except NetmikoAuthenticationException as exc:
        error_msg = f"SSH authentication failed: {str(exc)}"
        return _result(
            False,
            error=error_msg,
            device_id=device_payload.get("id"),
            device_name=device_payload.get("name"),
        )
    except Exception as exc:
        error_msg = f"Connection test failed: {str(exc)}"
        return _result(
            False,
            error=error_msg,
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
    """
    Execute configuration commands on multiple devices in parallel.
    
    Uses ThreadPoolExecutor for concurrent device connections.
    
    Args:
        device_list: List of device ORM models or dicts
        commands: Single command string or list of command strings
        timeout: SSH timeout per device
        max_workers: Number of concurrent threads
    
    Returns:
        List of result dicts, one per device in same order
    """
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

    # Build device objects map for enable_secret lookup
    device_obj_map = {}
    if device_list:
        for i, dev in enumerate(device_list):
            device_obj_map[i] = dev

    with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="netmiko-job") as executor:
        future_map = {
            executor.submit(
                push_config_commands, 
                serialized_devices[index], 
                commands, 
                timeout, 
                cfg
            ): index
            for index in range(len(serialized_devices))
        }
        for future in as_completed(future_map):
            index = future_map[future]
            try:
                ordered_results[index] = future.result()
            except Exception as exc:
                device = serialized_devices[index]
                device_obj = device_obj_map.get(index)
                ordered_results[index] = _result(
                    False,
                    error=f"Unhandled job execution error: {str(exc)}",
                    device_id=device.get("id"),
                    device_name=device.get("name"),
                    commands=commands,
                )
                current_app.logger.error(f"[AUTOMATION] Job execution failed for device {device.get('name')}: {exc}")

    return [item for item in ordered_results if item is not None]
