from __future__ import annotations

from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from flask import current_app

from ..db import db
from ..models.device import Device
from ..models.metrics import Metrics

try:
    from pysnmp.hlapi import (
        CommunityData,
        ContextData,
        ObjectIdentity,
        ObjectType,
        SnmpEngine,
        UdpTransportTarget,
        getCmd,
        nextCmd,
    )
except Exception:  # pragma: no cover - handled by runtime checks
    CommunityData = None
    ContextData = None
    ObjectIdentity = None
    ObjectType = None
    SnmpEngine = None
    UdpTransportTarget = None
    getCmd = None
    nextCmd = None


scheduler = BackgroundScheduler()

# Common SNMP OIDs
OID_CPU_5SEC = "1.3.6.1.4.1.9.2.1.57.0"
OID_MEM_USED = "1.3.6.1.4.1.2021.4.6.0"
OID_MEM_TOTAL = "1.3.6.1.4.1.2021.4.5.0"
OID_UPTIME = "1.3.6.1.2.1.1.3.0"
OID_IF_OPER_STATUS = "1.3.6.1.2.1.2.2.1.8"
OID_IF_IN_OCTETS = "1.3.6.1.2.1.2.2.1.10"
OID_IF_OUT_OCTETS = "1.3.6.1.2.1.2.2.1.16"


def _snmp_supported() -> bool:
    return all(
        [
            CommunityData,
            ContextData,
            ObjectIdentity,
            ObjectType,
            SnmpEngine,
            UdpTransportTarget,
            getCmd,
            nextCmd,
        ]
    )


def _snmp_get(ip: str, community: str, oid: str, timeout_seconds: int, retries: int) -> float | None:
    if not _snmp_supported():
        return None

    iterator = getCmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),  # v2c
        UdpTransportTarget((ip, 161), timeout=timeout_seconds, retries=retries),
        ContextData(),
        ObjectType(ObjectIdentity(oid)),
    )

    error_indication, error_status, _, var_binds = next(iterator)
    if error_indication or error_status:
        return None

    for _, value in var_binds:
        try:
            return float(value.prettyPrint())
        except Exception:
            return None
    return None


def _snmp_walk(ip: str, community: str, base_oid: str, timeout_seconds: int, retries: int) -> dict[int, float]:
    if not _snmp_supported():
        return {}

    results: dict[int, float] = {}
    iterator = nextCmd(
        SnmpEngine(),
        CommunityData(community, mpModel=1),  # v2c
        UdpTransportTarget((ip, 161), timeout=timeout_seconds, retries=retries),
        ContextData(),
        ObjectType(ObjectIdentity(base_oid)),
        lexicographicMode=False,
    )

    for error_indication, error_status, _, var_binds in iterator:
        if error_indication or error_status:
            break
        for oid_obj, value in var_binds:
            oid_text = oid_obj.prettyPrint()
            try:
                idx = int(oid_text.split(".")[-1])
                results[idx] = float(value.prettyPrint())
            except Exception:
                continue
    return results


def poll_device_metrics(device: Device, community: str, timeout_seconds: int = 2, retries: int = 1) -> list[dict]:
    ip = device.ip_address
    metrics: list[dict] = []

    cpu = _snmp_get(ip, community, OID_CPU_5SEC, timeout_seconds, retries)
    if cpu is not None:
        metrics.append({"metric_name": "cpu_usage", "value": cpu})

    mem_used = _snmp_get(ip, community, OID_MEM_USED, timeout_seconds, retries)
    mem_total = _snmp_get(ip, community, OID_MEM_TOTAL, timeout_seconds, retries)
    if mem_used is not None and mem_total and mem_total > 0:
        memory_pct = (mem_used / mem_total) * 100.0
        metrics.append({"metric_name": "memory_usage", "value": memory_pct})

    uptime = _snmp_get(ip, community, OID_UPTIME, timeout_seconds, retries)
    if uptime is not None:
        metrics.append({"metric_name": "uptime_ticks", "value": uptime})

    oper_status_map = _snmp_walk(ip, community, OID_IF_OPER_STATUS, timeout_seconds, retries)
    for if_index, status in oper_status_map.items():
        metrics.append({"metric_name": f"if_oper_status_{if_index}", "value": status})

    in_octets_map = _snmp_walk(ip, community, OID_IF_IN_OCTETS, timeout_seconds, retries)
    for if_index, counter in in_octets_map.items():
        metrics.append({"metric_name": f"if_in_octets_{if_index}", "value": counter})

    out_octets_map = _snmp_walk(ip, community, OID_IF_OUT_OCTETS, timeout_seconds, retries)
    for if_index, counter in out_octets_map.items():
        metrics.append({"metric_name": f"if_out_octets_{if_index}", "value": counter})

    return metrics


def poll_all_devices() -> int:
    community = current_app.config.get("SNMP_COMMUNITY", "public")
    timeout_seconds = int(current_app.config.get("SNMP_TIMEOUT_SECONDS", 2))
    retries = int(current_app.config.get("SNMP_RETRIES", 1))

    devices = Device.query.all()
    written = 0
    now = datetime.utcnow()

    for device in devices:
        device_metrics = poll_device_metrics(
            device=device,
            community=community,
            timeout_seconds=timeout_seconds,
            retries=retries,
        )
        for metric in device_metrics:
            db.session.add(
                Metrics(
                    device_id=device.id,
                    metric_name=metric["metric_name"],
                    value=float(metric["value"]),
                    timestamp=now,
                )
            )
            written += 1

    db.session.commit()
    return written


def start_snmp_monitor(app) -> None:
    if scheduler.running:
        return

    with app.app_context():
        interval = int(app.config.get("SNMP_POLL_INTERVAL_SECONDS", 60))

    def job_wrapper():
        with app.app_context():
            poll_all_devices()

    scheduler.add_job(job_wrapper, "interval", seconds=interval, id="snmp_monitor", replace_existing=True)
    scheduler.start()
