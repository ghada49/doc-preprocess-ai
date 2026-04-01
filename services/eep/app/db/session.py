"""
services/eep/app/db/session.py
-------------------------------
SQLAlchemy session factory for the EEP service.

Database URL is read from the DATABASE_URL environment variable.
Falls back to a local development URL if the variable is not set.

Exports:
    engine        — SQLAlchemy engine (created from DATABASE_URL)
    SessionLocal  — sessionmaker bound to the engine
    get_session   — FastAPI dependency generator that yields a Session
"""

from __future__ import annotations

import os
from collections.abc import Generator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

_DATABASE_URL: str = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://libraryai:changeme@localhost:5432/libraryai",
)
_DATABASE_URL = _DATABASE_URL.replace(
    "postgresql+asyncpg://",
    "postgresql+psycopg2://",
)

engine = create_engine(_DATABASE_URL)

SessionLocal: sessionmaker[Session] = sessionmaker(engine)


def get_session() -> Generator[Session, None, None]:
    """
    FastAPI dependency that yields a DB session and ensures it is closed.

    Usage::

        @app.get("/example")
        def example(db: Session = Depends(get_session)):
            ...
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
