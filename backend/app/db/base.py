"""
Single source of truth for the SQLAlchemy declarative base.

ALL models must import Base from here so that Base.metadata is consistent
across the app, Alembic migrations, and init_db().
"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass
