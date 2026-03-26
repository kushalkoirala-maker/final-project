import os


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-change-me")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///netops.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    DEVICE_SSH_USER = os.getenv("DEVICE_SSH_USER")
    DEVICE_SSH_PASS = os.getenv("DEVICE_SSH_PASS")
    DEVICE_ENABLE_SECRET = os.getenv("DEVICE_ENABLE_SECRET") or os.getenv("DEVICE_ENABLE_PASS")
    AUTOMATION_MAX_WORKERS = int(os.getenv("AUTOMATION_MAX_WORKERS", "10"))
    AUTOMATION_TIMEOUT_SECONDS = int(os.getenv("AUTOMATION_TIMEOUT_SECONDS", "20"))

    # Monitoring defaults
    MONITOR_INTERVAL_SECONDS = int(os.getenv("MONITOR_INTERVAL_SECONDS", "10"))
    MONITOR_PING_TIMEOUT_MS = int(os.getenv("MONITOR_PING_TIMEOUT_MS", "800"))

   # Zabbix configuration
    ZABBIX_URL = os.getenv("ZABBIX_URL", "http://192.168.64.135/zabbix")
    # Zabbix 7.0 uses API Tokens for modern authentication
    ZABBIX_API_TOKEN = os.getenv("ZABBIX_API_TOKEN", "ce39351415804804e3408d081082f11d6b4edb747b6ac9b8a46edf81f5fa597b")
    
    # You likely won't need Username/Password if you are using the Token
    ZABBIX_USERNAME = os.getenv("ZABBIX_USERNAME", "Admin")
    ZABBIX_PASSWORD = os.getenv("ZABBIX_PASSWORD", "zabbix")
    # Job worker defaults
    JOB_POLL_INTERVAL_SECONDS = int(os.getenv("JOB_POLL_INTERVAL_SECONDS", "3"))

    # SNMP monitoring defaults
    SNMP_COMMUNITY = os.getenv("SNMP_COMMUNITY", "public")
    SNMP_TIMEOUT_SECONDS = int(os.getenv("SNMP_TIMEOUT_SECONDS", "2"))
    SNMP_RETRIES = int(os.getenv("SNMP_RETRIES", "1"))
    SNMP_POLL_INTERVAL_SECONDS = int(os.getenv("SNMP_POLL_INTERVAL_SECONDS", "60"))
