"""
models/solution.py
------------------
SQLAlchemy ORM model for solutions table.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from db.base import Base


class SolutionModel(Base):
    __tablename__ = "solutions"

    id        : Mapped[int] = mapped_column(Integer,     primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    incident_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("incidents.id", ondelete="CASCADE"), nullable=False, index=True
    )

    source    : Mapped[Optional[str]]   = mapped_column(String(64),  nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float,       nullable=True)
    content   : Mapped[Optional[str]]   = mapped_column(Text,        nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    # ── Relationship ──────────────────────────────────────────────────────────
    incident: Mapped["IncidentModel"] = relationship(   # noqa: F821
        "IncidentModel", back_populates="solutions"
    )

    __table_args__ = (
        Index("ix_solutions_incident_id",  "incident_id"),
        Index("ix_solutions_confidence",   "confidence"),
        Index("ix_solutions_created_at",   "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"SolutionModel(id={self.id!r}, incident_id={self.incident_id!r}, "
            f"source={self.source!r}, confidence={self.confidence!r})"
        )
