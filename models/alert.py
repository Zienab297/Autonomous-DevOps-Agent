"""
models/alert.py
---------------
SQLAlchemy ORM model for alerts table.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class AlertModel(Base):
    __tablename__ = "alerts"

    id        : Mapped[int] = mapped_column(Integer,     primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title   : Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    severity: Mapped[Optional[str]] = mapped_column(String(32),  nullable=True)
    channel : Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sent    : Mapped[bool]          = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime]           = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[Optional[datetime]]    = mapped_column(DateTime(timezone=True), nullable=True)

    # ── Relationship ──────────────────────────────────────────────────────────
    incident: Mapped["IncidentModel"] = relationship(   # noqa: F821
        "IncidentModel", back_populates="alerts"
    )

    __table_args__ = (
        Index("ix_alerts_incident_id", "incident_id"),
        Index("ix_alerts_channel",     "channel"),
        Index("ix_alerts_created_at",  "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"AlertModel(id={self.id!r}, incident_id={self.incident_id!r}, "
            f"severity={self.severity!r}, channel={self.channel!r})"
        )
