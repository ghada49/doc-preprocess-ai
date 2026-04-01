"""
services/eep/migrations/env.py
-------------------------------
Alembic environment configuration for the EEP service.

Database URL is read from the DATABASE_URL environment variable.
Expected format: postgresql+psycopg2://user:pass@host:port/dbname

Both online (live DB) and offline (generate SQL only) modes are supported.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from services.eep.app.db.models import Base

# Alembic Config object (gives access to alembic.ini values)
config = context.config

# Honour logging config in alembic.ini
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Read DATABASE_URL from environment; fall back to a placeholder so that
# alembic commands that don't need a live DB (e.g. `alembic revision`) still
# work without raising a configuration error.
_database_url = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://libraryai:changeme@localhost:5432/libraryai",
)
_database_url = _database_url.replace(
    "postgresql+asyncpg://",
    "postgresql+psycopg2://",
)
config.set_main_option("sqlalchemy.url", _database_url)

# ORM metadata used by Alembic for autogenerate support (Packet 1.6+).
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Configures the context with just a URL, without creating an Engine.
    Useful for generating SQL scripts without a live database connection.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
