# NetOps Automation Platform - Quick Reference Guide

## Environment Setup

```bash
# 1. Create .env file in project root
cat > .env << EOF
SECRET_KEY=your-secret-key-here
DATABASE_URL=sqlite:///netops.db
DEVICE_SSH_USER=admin
DEVICE_SSH_PASS=your_ssh_password
DEVICE_ENABLE_SECRET=your_enable_password
MONITOR_INTERVAL_SECONDS=10
MONITOR_SSH_TIMEOUT_SECONDS=15
MONITOR_MAX_WORKERS=5
AUTOMATION_MAX_WORKERS=10
EOF

# 2. Initialize database
python run.py
# In another terminal:
flask db init
flask db migrate
flask db upgrade

# 3. Verify imports work
python -c "from netmiko import ConnectHandler; print('Netmiko OK')"
python -c "from app import create_app; print('Flask app OK')"
```

## Testing SSH Connection

```python
# In Python shell while app is running
flask shell

from app.models.device import Device
from app.services.automation_service import test_connection

# Create test device (or use existing)
device = Device(
    name="TestRouter",
    ip_address="192.168.1.1",
    vendor="cisco",
    device_type="router"
)

# Test connection
result = test_connection(device)
print(result)
# Should output: {'success': True, 'output': '...', 'prompt': 'Router#', ...}
```

## Running Configuration Jobs

```python
from app.models.device import Device
from app.services.automation_service import push_config_commands

device = Device.query.filter_by(name="Router1").first()
commands = [
    "interface GigabitEthernet0/0/1",
    "description Updated via NetOps",
    "no shutdown",
]

result = push_config_commands(device, commands)
print(result)
# Should output: {'success': True, 'output': '...', 'commands': [...], ...}
```

## Monitoring Verification

```python
# Verify metrics are being collected
from app.db import db
from app.models.metrics import Metrics
from datetime import datetime, timedelta

# Check metrics from last 5 minutes
five_min_ago = datetime.utcnow() - timedelta(minutes=5)
recent_metrics = Metrics.query.filter(Metrics.timestamp > five_min_ago).all()
print(f"Collected {len(recent_metrics)} metrics in last 5 minutes")

# Check latest CPU for device 1
latest_cpu = (Metrics.query
    .filter_by(device_id=1, metric_name='cpu_usage')
    .order_by(Metrics.timestamp.desc())
    .first())
print(f"Device 1 CPU: {latest_cpu.value}%")
```

## Dashboard URLs

- Home: http://localhost:5000/
- Dashboard: http://localhost:5000/dashboard
- Monitoring: http://localhost:5000/monitoring
- Devices: http://localhost:5000/devices
- Jobs: http://localhost:5000/jobs
- Alerts: http://localhost:5000/alerts
- Admin Users: http://localhost:5000/admin/users

## Common Commands

```bash
# Start Flask development server
python run.py

# Run in production with gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 "app:create_app()"

# Run tests
pytest tests/

# Generate migration for model changes
flask db migrate -m "Add enable_secret to Device"

# View database schema
sqlite3 netops.db ".schema device"

# Reset database (development only)
rm netops.db
flask db upgrade
```

## MIB/Netmiko Compatibility

Device types supported:
- cisco_ios (Cisco IOS routers/switches)
- cisco_nxos (Nexus devices)
- arista_eos (Arista EOS)
- juniper_junos (Juniper devices)
- linux (Linux hosts)

For other vendors, add mapping in `automation_service.py::netmiko_device_type()`

## Troubleshooting Commands

```bash
# Check if Netmiko can reach device
python -c "
from netmiko import ConnectHandler
net_connect = ConnectHandler(
    device_type='cisco_ios',
    host='192.168.1.1',
    username='admin',
    password='pass',
    secret='enable_pass',
    timeout=10
)
net_connect.enable()
print(net_connect.find_prompt())
net_connect.disconnect()
"

# Test regex parsing
python -c "
from app.services.monitor import _parse_cisco_cpu
output = '''
CPU utilization for five seconds: 25%; one minute: 20%; five minutes: 15%
'''
print(_parse_cisco_cpu(output))  # Should print 25.0
"

# Check thread pool status
python -c "
from concurrent.futures import ThreadPoolExecutor
with ThreadPoolExecutor(max_workers=5) as executor:
    import threading
    print(f'Active threads: {threading.active_count()}')
"
```

## Performance Tuning

- Increase MONITOR_MAX_WORKERS for more parallel SSH connections (default: 5)
- Decrease MONITOR_INTERVAL_SECONDS for more frequent polls (default: 10)
- Increase MONITOR_SSH_TIMEOUT_SECONDS for slow networks (default: 15)
- Configure database indexes for large scale deployments
- Consider archiving old Metrics entries (> 30 days) to maintain performance

## Production Deployment Checklist

- [ ] Use strong SECRET_KEY (min 32 characters)
- [ ] Set SQLALCHEMY_DATABASE_URI to PostgreSQL/MySQL (not SQLite)
- [ ] Configure HTTPS/SSL certificates
- [ ] Set up monitoring for Flask app crashes
- [ ] Configure log rotation for large deployments
- [ ] Implement Metrics table retention policy
- [ ] Test failover scenarios (device offline, network issues)
- [ ] Load test with expected concurrent device count
- [ ] Document custom vendor/device type mappings
- [ ] Backup database regularly

For additional help, see REFACTORING_COMPLETE.md or contact your instructor.
