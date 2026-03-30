"""
models/incident.py
------------------
SQLAlchemy ORM model for incidents table.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class IncidentModel(Base):
    __tablename__ = "incidents"

    # ── Primary key ───────────────────────────────────────────────────────────
    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    # ── Tenant / project ─────────────────────────────────────────────────────
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # ── Core fields ───────────────────────────────────────────────────────────
    service    : Mapped[str]           = mapped_column(String(255), nullable=False)
    severity   : Mapped[str]           = mapped_column(String(32),  nullable=False)
    status     : Mapped[str]           = mapped_column(String(32),  nullable=False, default="open")
    description: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)

    # ── Timestamps ────────────────────────────────────────────────────────────
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    # ── Relationships ─────────────────────────────────────────────────────────
    solutions: Mapped[List["SolutionModel"]] = relationship(   # noqa: F821
        "SolutionModel",
        back_populates="incident",
        cascade="all, delete-orphan",
    )
    actions: Mapped[List["ActionModel"]] = relationship(        # noqa: F821
        "ActionModel",
        back_populates="incident",
        cascade="all, delete-orphan",
    )
    alerts: Mapped[List["AlertModel"]] = relationship(          # noqa: F821
        "AlertModel",
        back_populates="incident",
        cascade="all, delete-orphan",
    )

    # ── Indexes ───────────────────────────────────────────────────────────────
    __table_args__ = (
        Index("ix_incidents_project_service",  "project_id", "service"),
        Index("ix_incidents_project_status",   "project_id", "status"),
        Index("ix_incidents_created_at",       "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"IncidentModel(id={self.id!r}, service={self.service!r}, "
            f"severity={self.severity!r}, status={self.status!r})"
        )
