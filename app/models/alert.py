from ..db import db


class Alert(db.Model):
    __tablename__ = "alert"
    __table_args__ = (
        db.CheckConstraint("severity IN ('info', 'warn', 'crit')", name="ck_alert_severity"),
        db.Index("ix_alert_device_created_at", "device_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    severity = db.Column(db.String(10), nullable=False, default="info")
    message = db.Column(db.String(255), nullable=False)
    device_id = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=True, index=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<Alert id={self.id} severity={self.severity!r} device_id={self.device_id}>"
