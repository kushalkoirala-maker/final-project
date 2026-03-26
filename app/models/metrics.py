from ..db import db


class Metrics(db.Model):
    __tablename__ = "metrics"
    __table_args__ = (
        db.Index("ix_metrics_device_metric_timestamp", "device_id", "metric_name", "timestamp"),
    )

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=False, index=True)
    metric_name = db.Column(db.String(64), nullable=False, index=True)
    value = db.Column(db.Float, nullable=False)
    timestamp = db.Column(db.DateTime, server_default=db.func.now(), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<Metrics id={self.id} metric_name={self.metric_name!r} device_id={self.device_id}>"
