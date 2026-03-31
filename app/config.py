import os


class Config:
    """
    NetOps Automation Platform Configuration - 100% Standalone (No Zabbix Dependency)
    
    All monitoring, alerting, and metrics are handled directly via SSH using Netmiko,
    with multithreaded polling and local metrics storage in SQLAlchemy.
    """
    
    # ==== Core Application Settings ====
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///netops.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # ==== Primary SSH Credentials for All Device Management ====
    # These are GLOBAL defaults; individual devices can override via enable_secret column
    DEVICE_SSH_USER = os.getenv("DEVICE_SSH_USER")
    DEVICE_SSH_PASS = os.getenv("DEVICE_SSH_PASS")
    DEVICE_ENABLE_SECRET = os.getenv("DEVICE_ENABLE_SECRET") or os.getenv("DEVICE_ENABLE_PASS")
    
    # ==== Automation Service (Config Push) Settings ====
    # Concurrent device connections for configuration deployment
    AUTOMATION_MAX_WORKERS = int(os.getenv("AUTOMATION_MAX_WORKERS", "10"))
    # SSH timeout for each device connection during config operations
    AUTOMATION_TIMEOUT_SECONDS = int(os.getenv("AUTOMATION_TIMEOUT_SECONDS", "20"))

    # ==== Standalone SSH Monitoring (Direct Netmiko-Based) ====
    # Polling interval in seconds for device metrics collection
    MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "10"))
    # SSH timeout per device for data collection
    MONITOR_SSH_TIMEOUT_SECONDS = int(os.getenv("MONITOR_SSH_TIMEOUT_SECONDS", "15"))
    # ICMP ping timeout (used as fallback if SSH fails)
    MONITOR_PING_TIMEOUT_MS = int(os.getenv("MONITOR_PING_TIMEOUT_MS", "800"))
    # Number of concurrent worker threads for parallel device polling
    MONITOR_MAX_WORKERS = int(os.getenv("MONITOR_MAX_WORKERS", "5"))
    
    # ==== Job Worker (Background Task Processing) ====
    # Polling interval for job status updates
    JOB_POLL_INTERVAL_SECONDS = int(os.getenv("JOB_POLL_INTERVAL_SECONDS", "3"))
