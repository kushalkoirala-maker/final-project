from datetime import datetime
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from ..db import db


class User(db.Model, UserMixin):
    __tablename__ = "user"
    __table_args__ = (
        db.CheckConstraint(
            "role IN ('admin', 'operator', 'viewer')",
            name="ck_user_role",
        ),
        db.Index("ix_user_role", "role"),
        db.Index("ix_user_active", "active"),
    )

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="viewer")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self) -> bool:
        return self.active

    @property
    def is_disabled(self) -> bool:
        return not self.active

    @staticmethod
    def create_admin_default():
        u = User(username="admin", role="admin", active=True)
        u.set_password("admin123")  # change later
        return u

    def __repr__(self) -> str:
        """Return a developer-friendly representation for Flask shell debugging."""
        return (
            f"<User(id={self.id}, username={self.username!r}, role={self.role!r}, "
            f"active={self.active!r}, created_at={self.created_at!r})>"
        )
