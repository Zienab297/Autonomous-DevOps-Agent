"""
models/action.py
----------------
SQLAlchemy ORM model for remediation actions table.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class ActionModel(Base):
    __tablename__ = "actions"

    id        : Mapped[int] = mapped_column(Integer,     primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    command: Mapped[Optional[str]] = mapped_column(Text,       nullable=True)
    status : Mapped[str]           = mapped_column(String(32), nullable=False, default="pending")
    output : Mapped[Optional[str]] = mapped_column(Text,       nullable=True)
    error  : Mapped[Optional[str]] = mapped_column(Text,       nullable=True)

    created_at : Mapped[datetime]           = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    executed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Relationship ──────────────────────────────────────────────────────────
    incident: Mapped["IncidentModel"] = relationship(   # noqa: F821
        "IncidentModel", back_populates="actions"
    )

    __table_args__ = (
        Index("ix_actions_incident_id", "incident_id"),
        Index("ix_actions_status",      "status"),
        Index("ix_actions_created_at",  "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"ActionModel(id={self.id!r}, incident_id={self.incident_id!r}, "
            f"status={self.status!r})"
        )
