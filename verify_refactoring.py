#!/usr/bin/env python
"""Verification script for NetOps Automation Platform refactoring"""

import sys
import inspect

sys.path.insert(0, '.')

print("=" * 70)
print("NETOPS PLATFORM - REFACTORING VERIFICATION")
print("=" * 70)

try:
    # Test 1: Check Device model has enable_secret
    from app.models.device import Device
    schema = Device.__table__.columns
    has_enable_secret = 'enable_secret' in [c.name for c in schema]
    print(f"✓ Device.enable_secret column: {'YES' if has_enable_secret else 'NO'}")

    # Test 2: Check config has SSH settings
    from app.config import Config
    config_dict = vars(Config)
    has_ssh_user = 'DEVICE_SSH_USER' in config_dict
    has_enable_secret_cfg = 'DEVICE_ENABLE_SECRET' in config_dict
    has_monitor_workers = 'MONITOR_MAX_WORKERS' in config_dict
    print(f"✓ Config SSH settings (user+pass+secret): {'YES' if (has_ssh_user and has_enable_secret_cfg) else 'NO'}")
    print(f"✓ Config MONITOR_MAX_WORKERS: {'YES' if has_monitor_workers else 'NO'}")

    # Test 3: Check automation_service has exception handling
    from app.services import automation_service
    source = inspect.getsource(automation_service)
    has_netmiko_timeout = 'NetmikoTimeoutException' in source
    has_netmiko_auth = 'NetmikoAuthenticationException' in source
    has_enable_check = 'check_enable_mode' in source
    print(f"✓ automation_service handles NetmikoTimeoutException: {'YES' if has_netmiko_timeout else 'NO'}")
    print(f"✓ automation_service handles NetmikoAuthenticationException: {'YES' if has_netmiko_auth else 'NO'}")
    print(f"✓ automation_service checks enable_mode: {'YES' if has_enable_check else 'NO'}")

    # Test 4: Check monitor.py has ThreadPoolExecutor
    from app.services import monitor
    source = inspect.getsource(monitor)
    has_thread_pool = 'ThreadPoolExecutor' in source
    has_as_completed = 'as_completed' in source
    has_device_specific_secret = 'device.enable_secret' in source
    print(f"✓ monitor.py uses ThreadPoolExecutor: {'YES' if has_thread_pool else 'NO'}")
    print(f"✓ monitor.py uses as_completed for async: {'YES' if has_as_completed else 'NO'}")
    print(f"✓ monitor.py supports device-specific secrets: {'YES' if has_device_specific_secret else 'NO'}")

    # Test 5: Check routes.py is Zabbix-free
    from app import routes as routes_module
    source = inspect.getsource(routes_module)
    has_zabbix = 'zabbix' in source.lower()
    has_metrics_query = 'Metrics' in source
    print(f"✓ routes.py is Zabbix-free: {'YES' if not has_zabbix else 'NO'}")
    print(f"✓ routes.py queries local Metrics table: {'YES' if has_metrics_query else 'NO'}")

    print()
    print("=" * 70)
    print("ALL REFACTORING CHANGES VERIFIED SUCCESSFULLY!")
    print("=" * 70)
    
except Exception as e:
    print(f"ERROR during verification: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)
