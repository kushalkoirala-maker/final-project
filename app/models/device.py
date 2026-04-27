from ..db import db


class Device(db.Model):
    """
    Device model for network equipment management.
    
    Enterprise Features:
    - last_seen: Operational depth tracking for monitoring pipeline health
    - ssh_port: Support for non-standard management ports
    - description: Administrative audit trail for device context
    - enable_secret: Device-specific enable password for privilege mode
    """
    __tablename__ = "device"
    __table_args__ = (
        db.Index("ix_device_name", "name"),
        db.Index("ix_device_created_at", "created_at"),
        db.Index("ix_device_last_seen", "last_seen"),
        db.Index("ix_device_is_up", "is_up"),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    ip_address = db.Column(db.String(64), nullable=False, unique=True, index=True)
    device_type = db.Column(db.String(50), nullable=False, default="router")
    vendor = db.Column(db.String(50), nullable=False, default="cisco")
    location = db.Column(db.String(120), nullable=True)
    # Enterprise: SSH port configuration for non-standard management access
    ssh_port = db.Column(db.Integer, nullable=False, default=22)
    # Enterprise: Administrative notes for device context and history
    description = db.Column(db.Text, nullable=True)
    # Enterprise: Device-specific enable secret (falls back to CONFIG if not set)
    enable_secret = db.Column(db.String(255), nullable=True)
    is_up = db.Column(db.Boolean, nullable=False, default=False)
    # Enterprise: Degraded status flag (SSH failed but ICMP ping succeeded)
    degraded_status = db.Column(db.Boolean, nullable=False, default=False)
    # Enterprise: Operational depth - timestamp when monitoring pipeline last reached device
    last_seen = db.Column(db.DateTime, nullable=True, default=None)
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False)

    snapshots = db.relationship(
        "ConfigSnapshot",
        backref="device",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="desc(ConfigSnapshot.created_at), desc(ConfigSnapshot.id)",
    )
    jobs = db.relationship(
        "Job",
        backref="device",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="desc(Job.created_at), desc(Job.id)",
    )
    alerts = db.relationship(
        "Alert",
        backref="device",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="desc(Alert.created_at), desc(Alert.id)",
    )
    metrics = db.relationship(
        "Metrics",
        backref="device",
        cascade="all, delete-orphan",
        lazy="select",
        order_by="desc(Metrics.timestamp), desc(Metrics.id)",
    )

    def __repr__(self) -> str:
        status = "degraded" if self.degraded_status else ("up" if self.is_up else "down")
        last_seen_str = self.last_seen.isoformat() if self.last_seen else "never"
        return (
            f"<Device(id={self.id}, name={self.name!r}, ip_address={self.ip_address!r}, "
            f"status={status}, ssh_port={self.ssh_port}, last_seen={last_seen_str})>"
        )
