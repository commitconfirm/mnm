"""Alembic environment for the mnm_controller database.

Uses the controller's async SQLAlchemy setup (asyncpg) — async engine is
created by env.py, and Alembic runs each migration via ``connection.run_sync``
on the async connection.  The DSN comes from the same MNM_DB_* environment
variables used by the application at runtime (see ``app.db._build_dsn``).

Imports ``app.db`` to register every model on ``Base.metadata``, so
``alembic revision --autogenerate`` introspects the full current schema.
"""
import asyncio

from sqlalchemy import pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Import the declarative Base and DSN builder from the application.  The
# ``import app.db`` side-effect is what registers every table on Base.metadata;
# importing it as ``_``-prefixed is enough since we use Base.metadata directly.
from app.db import Base, _build_dsn  # noqa: E402
import app.db  # noqa: F401, E402  — side-effect: model class registration

# Alembic Config object, provides access to the values within the .ini file.
config = context.config

# Override the placeholder ``sqlalchemy.url`` in alembic.ini with the DSN
# built from MNM_DB_* environment variables at runtime.  This keeps the ini
# file free of credentials and matches how the application connects.
config.set_main_option("sqlalchemy.url", _build_dsn())

# NOTE: alembic's stock template calls ``fileConfig(config.config_file_name)``
# here.  The controller intentionally does NOT.  ``fileConfig`` defaults to
# ``disable_existing_loggers=True`` and replaces root logger handlers per
# alembic.ini's ``[logger_root]`` block, which clobbers the StructuredFormatter
# handler that ``app.logging_config.setup_logging()`` installs at controller
# import time.  See .claude/investigations/v1-pre-tag-2026-05-06.md (Round 3).
# The application owns logger configuration; alembic logs propagate through
# the root logger and reach the StructuredFormatter without any alembic-side
# config.

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emits SQL to stdout, no DB)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Synchronous migration runner invoked inside ``connection.run_sync``."""
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and hand a sync-wrapped connection to Alembic."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live database."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
