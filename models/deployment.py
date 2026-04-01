"""models/deployment.py — SQLAlchemy ORM model for deployments table."""

from datetime import datetime
from typing   import Optional

from sqlalchemy import DateTime, Index, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class DeploymentModel(Base):
    __tablename__ = "deployments"

    id        : Mapped[str] = mapped_column(String(64),  primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    service     : Mapped[str]           = mapped_column(String(255), nullable=False)
    branch      : Mapped[str]           = mapped_column(String(255), nullable=False)
    version     : Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    status      : Mapped[str]           = mapped_column(String(32),  nullable=False, default="pending")
    conclusion  : Mapped[Optional[str]] = mapped_column(String(32),  nullable=True)
    pipeline_url: Mapped[Optional[str]] = mapped_column(Text,        nullable=True)
    run_id      : Mapped[Optional[str]] = mapped_column(String(64),  nullable=True)

    started_at : Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at : Mapped[datetime]           = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_deployments_project_service", "project_id", "service"),
        Index("ix_deployments_status",          "status"),
        Index("ix_deployments_created_at",      "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"DeploymentModel(id={self.id!r}, service={self.service!r}, "
            f"status={self.status!r}, conclusion={self.conclusion!r})"
        )