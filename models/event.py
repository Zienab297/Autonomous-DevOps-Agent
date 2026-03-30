"""
models/event.py
---------------
SQLAlchemy ORM model for event_logs table.
Full audit trail of every EventBus event.
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import DateTime, Index, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from db.base import Base


class EventLogModel(Base):
    __tablename__ = "event_logs"

    id        : Mapped[int] = mapped_column(Integer,     primary_key=True, autoincrement=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    type       : Mapped[str]           = mapped_column(String(128), nullable=False)
    source     : Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    incident_id: Mapped[Optional[str]] = mapped_column(String(64),  nullable=True, index=True)
    data_json  : Mapped[Optional[str]] = mapped_column(Text,        nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        Index("ix_event_logs_type",        "type"),
        Index("ix_event_logs_project_type","project_id", "type"),
        Index("ix_event_logs_created_at",  "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"EventLogModel(id={self.id!r}, type={self.type!r}, "
            f"source={self.source!r})"
        )
