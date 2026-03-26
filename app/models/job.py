import json

from ..db import db


class Job(db.Model):
    """
    Job model for automation task execution and tracking.
    
    Enterprise Features:
    - user_id: Auditability - every automation action traceable to a user
    - error_summary: Quick dashboard viewing vs raw execution logs
    - device_id (nullable): Supports both single and multi-device targeting
    """
    __tablename__ = "job"
    __table_args__ = (
        db.Index("ix_job_status", "status"),
        db.Index("ix_job_created_at", "created_at"),
        db.Index("ix_job_user_id", "user_id"),
        db.Index("ix_job_device_id", "device_id"),
    )

    id = db.Column(db.Integer, primary_key=True)
    # Enterprise: Audit trail - track which administrator executed this job
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    # Enterprise: Nullable device_id supports multi-device targeting via device_ids_json
    # Logic: If device_ids_json contains multiple IDs, device_id acts as optional primary reference or is null
    device_id = db.Column(db.Integer, db.ForeignKey("device.id"), nullable=True)
    device_ids_json = db.Column(db.Text, nullable=True)
    type = db.Column(db.String(50), nullable=False)
    payload_json = db.Column(db.Text, nullable=False)
    device_results_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="pending")  # pending/running/success/failed
    # Enterprise: High-level failure reason for quick dashboard viewing (distinct from raw logs)
    error_summary = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, server_default=db.func.now())
    started_at = db.Column(db.DateTime, nullable=True)
    finished_at = db.Column(db.DateTime, nullable=True)
    result_text = db.Column(db.Text, nullable=True)

    # Relationship to User for audit trail
    user = db.relationship(
        "User",
        backref="jobs",
        foreign_keys=[user_id],
    )

    def get_device_ids(self) -> list[int]:
        if self.device_ids_json:
            try:
                parsed = json.loads(self.device_ids_json)
                if isinstance(parsed, list):
                    ids = []
                    for value in parsed:
                        try:
                            ids.append(int(value))
                        except Exception:
                            continue
                    if ids:
                        return sorted(set(ids))
            except Exception:
                pass
        return [self.device_id] if self.device_id is not None else []

    def set_device_ids(self, device_ids: list[int]) -> None:
        values = sorted({int(v) for v in (device_ids or [])})
        self.device_ids_json = json.dumps(values)
        if values:
            # Backward-compatible primary device pointer.
            self.device_id = values[0]

    def has_device(self, device_id: int) -> bool:
        return int(device_id) in set(self.get_device_ids())

    def get_device_results(self) -> list[dict]:
        if not self.device_results_json:
            return []
        try:
            parsed = json.loads(self.device_results_json)
            if isinstance(parsed, list):
                return parsed
        except Exception:
            return []
        return []

    def set_device_results(self, results: list[dict]) -> None:
        self.device_results_json = json.dumps(results or [])

    def __repr__(self) -> str:
        device_info = f"device_id={self.device_id}" if self.device_id else "multi-device"
        duration = ""
        if self.started_at and self.finished_at:
            delta = (self.finished_at - self.started_at).total_seconds()
            duration = f", duration={delta:.1f}s"
        return (
            f"<Job(id={self.id}, user_id={self.user_id}, {device_info}, "
            f"type={self.type!r}, status={self.status}{duration})>"
        )
