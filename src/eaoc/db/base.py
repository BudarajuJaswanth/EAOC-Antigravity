"""
EAOC Database Base Module
Defines SQLAlchemy DeclarativeBase for future ORM models.
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """
    Base class for all EAOC SQLAlchemy ORM models.
    Provides metadata registry for database tables and Alembic migrations.
    """
    pass
