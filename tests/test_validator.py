from app.services.validator import validate_commands


def test_vlan_id_range_valid():
    result = validate_commands(
        ["vlan 10", "interface Gi1/0/1", "switchport access vlan 10"],
        variables={"vlan_id": 10},
    )
    assert result["passed"] is True
    assert result["reasons"] == []


def test_vlan_id_range_invalid_from_variables():
    result = validate_commands(["vlan 5000"], variables={"vlan_id": 5000})
    assert result["passed"] is False
    assert any("VLAN ID out of range" in reason for reason in result["reasons"])


def test_ip_address_format_invalid():
    result = validate_commands(
        ["ip route 10.10.10.0 255.255.255.0 999.1.1.1"],
        variables={"next_hop": "999.1.1.1"},
    )
    assert result["passed"] is False
    assert any("Invalid IP address format: 999.1.1.1" in reason for reason in result["reasons"])


def test_risky_commands_blocked_by_default():
    result = validate_commands(["interface Gi1/0/1", "shutdown", "no ip routing"])
    assert result["passed"] is False
    assert any("Risky commands blocked" in reason for reason in result["reasons"])


def test_risky_commands_allowed_with_flag():
    result = validate_commands(
        ["interface Gi1/0/1", "shutdown", "no ip routing"],
        allow_risky_commands=True,
    )
    assert result["passed"] is True


def test_acl_deny_any_any_without_prior_permit_blocked():
    result = validate_commands([
        "ip access-list extended EDGE-FILTER",
        "deny ip any any",
        "permit ip any any",
    ])
    assert result["passed"] is False
    assert any("deny ip any any" in reason for reason in result["reasons"])


def test_acl_deny_any_any_with_prior_permit_allowed():
    result = validate_commands([
        "ip access-list extended EDGE-FILTER",
        "permit ip any any",
        "deny ip any any",
    ])
    assert result["passed"] is True


def test_multiple_failures_reported_together():
    result = validate_commands(
        ["vlan 4095", "shutdown", "deny ip any any"],
        variables={"next_hop": "300.1.1.1"},
    )
    assert result["passed"] is False
    assert len(result["reasons"]) >= 3
