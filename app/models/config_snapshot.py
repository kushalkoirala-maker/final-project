from ..db import db


class ConfigSnapshot(db.Model):
    __tablename__ = "config_snapshot"
    __table_args__ = (
        db.Index("ix_config_snapshot_device_created_at", "device_id", "created_at"),
        db.Index("ix_config_snapshot_config_hash", "config_hash"),
    )

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False, index=True)
    config_text = db.Column(db.Text, nullable=False)
    config_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, server_default=db.func.now(), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<ConfigSnapshot id={self.id} device_id={self.device_id} hash={self.config_hash[:8]!r}>"
