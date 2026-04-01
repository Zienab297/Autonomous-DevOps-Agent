"""
db/base.py
----------
Shared SQLAlchemy declarative base.
All ORM models must inherit from Base.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass