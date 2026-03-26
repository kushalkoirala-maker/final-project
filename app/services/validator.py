import ipaddress
import re
from typing import Any
from flask import current_app

def _extract_vlan_ids_from_commands(commands: list[str]) -> list[int]:
    patterns = [
        re.compile(r"^\s*vlan\s+(\d+)\s*$", re.IGNORECASE),
        re.compile(r"^\s*switchport\s+access\s+vlan\s+(\d+)\s*$", re.IGNORECASE),
        re.compile(r"^\s*switchport\s+trunk\s+native\s+vlan\s+(\d+)\s*$", re.IGNORECASE),
    ]
    vlan_ids: list[int] = []
    for command in commands:
        for pattern in patterns:
            match = pattern.search(command or "")
            if match:
                try:
                    vlan_ids.append(int(match.group(1)))
                except ValueError:
                    continue
    return vlan_ids

def _extract_vlan_ids_from_variables(variables: dict[str, Any]) -> list[int]:
    vlan_ids: list[int] = []
    for key, value in (variables or {}).items():
        key_l = str(key).lower()
        if key_l in {"vlan_id", "native_vlan"}:
            try:
                vlan_ids.append(int(value))
            except (TypeError, ValueError):
                vlan_ids.append(-1)
    return vlan_ids

def _collect_possible_ips(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        # Basic regex for IPv4
        matches = re.findall(r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}", value)
        found.extend(matches)
    elif isinstance(value, list):
        for item in value:
            found.extend(_collect_possible_ips(item))
    elif isinstance(value, dict):
        for v in value.values():
            found.extend(_collect_possible_ips(v))
    return found

def _is_valid_ipv4(ip: str) -> bool:
    try:
        ipaddress.IPv4Address(ip)
        return True
    except ValueError:
        return False

def _contains_risky_command(commands: list[str]) -> list[str]:
    """
    Identifies dangerous commands that could disconnect management 
    or reload the device.
    """
    risky_patterns = [
        (re.compile(r"^\s*reload\s*$", re.IGNORECASE), "reload"),
        (re.compile(r"^\s*erase\s+startup-config", re.IGNORECASE), "erase startup-config"),
        (re.compile(r"^\s*no\s+ip\s+routing", re.IGNORECASE), "no ip routing"),
        (re.compile(r"^\s*shutdown\s*$", re.IGNORECASE), "shutdown (interface disable)"),
    ]
    found = []
    for cmd in commands:
        for pattern, label in risky_patterns:
            if pattern.search(cmd or ""):
                found.append(label)
    return list(set(found))

def _acl_denied_without_permit(commands: list[str]) -> bool:
    saw_prior_permit = False
    for command in commands:
        normalized = " ".join((command or "").strip().lower().split())
        if not normalized:
            continue
        if "permit ip any any" in normalized:
            saw_prior_permit = True
        if "deny ip any any" in normalized and not saw_prior_permit:
            return True
    return False

def validate_commands(
    commands: list[str],
    variables: dict[str, Any] | None = None,
    allow_risky_commands: bool = False,
) -> dict:
    reasons: list[str] = []
    variables = variables or {}
    commands = commands or []

    # 1. VLAN Validation
    vlan_ids = _extract_vlan_ids_from_variables(variables) + _extract_vlan_ids_from_commands(commands)
    for vlan_id in vlan_ids:
        if not (1 <= vlan_id <= 4094):
            reasons.append(f"VLAN ID out of range (1-4094): {vlan_id}")

    # 2. IP Address Validation
    candidate_ips = _collect_possible_ips(variables) + _collect_possible_ips(commands)
    for ip in candidate_ips:
        if not _is_valid_ipv4(ip):
            reasons.append(f"Invalid IP address format: {ip}")

    # 3. Risky Command Check
    risky_found = _contains_risky_command(commands)
    if risky_found and not allow_risky_commands:
        reasons.append(
            f"Risky commands blocked: {', '.join(risky_found)}. "
            "Set allow_risky_commands=True to override."
        )

    # 4. ACL Logic Check
    if _acl_denied_without_permit(commands):
        reasons.append("Security risk: 'deny ip any any' detected without a preceding 'permit' statement.")

    validation_passed = len(reasons) == 0

    if not validation_passed:
        current_app.logger.warning(f"[VALIDATION] Failed: {reasons}")
    else:
        current_app.logger.debug(f"[VALIDATION] Passed: {len(commands)} commands")

    return {
        "passed": validation_passed,
        "reasons": reasons,
        "risky_commands": risky_found,
        "allow_risky_commands": allow_risky_commands
    }