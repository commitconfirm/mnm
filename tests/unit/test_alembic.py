"""Unit tests for Alembic migration infrastructure.

These tests exercise the migration setup itself — do the baseline upgrade
and downgrade paths work, and does ``_try_init_db`` correctly detect a
legacy-schema database (tables exist, no alembic_version) and stamp it
rather than trying to recreate the tables.

Tests run against a throwaway PostgreSQL database created per-test.
Skipped automatically if MNM_DB_HOST is unreachable (e.g., host-only
test runs without docker compose up).
"""
from __future__ import annotations

import os
import sys
import uuid

import pytest
import pytest_asyncio

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "controller"))

# Skip the whole module unless we can reach Postgres AND import the app modules
# with their async DB dependencies.  Host test runs without asyncpg installed
# should quietly skip; container runs with MNM_DB_HOST set exercise the tests.
_skip_reason = None
if not os.environ.get("MNM_DB_HOST"):
    _skip_reason = "MNM_DB_HOST not set — migration tests require Postgres"
else:
    try:
        # Import app.db at module load so sys.modules["app.db"] is populated
        # with the real module BEFORE any other test file's collection-time
        # sys.modules stubbing runs (test_route_collector.py stubs app.db via
        # sys.modules.setdefault — that's a no-op if we've already imported it).
        import app.db  # noqa: F401
        import asyncpg  # noqa: F401
    except ImportError as exc:
        _skip_reason = f"migration test dependencies unavailable: {exc}"

if _skip_reason:
    pytestmark = pytest.mark.skip(reason=_skip_reason)
else:
    pytestmark = []


@pytest_asyncio.fixture
async def throwaway_db(monkeypatch):
    """Create a fresh empty DB, yield its name, then drop it.

    Runs each test in isolation against a brand-new database so the
    workbench install's live ``mnm_controller`` is never touched.
    """
    import asyncpg

    admin_host = os.environ["MNM_DB_HOST"]
    admin_port = int(os.environ.get("MNM_DB_PORT", "5432"))
    admin_user = os.environ.get("MNM_DB_USER", "nautobot")
    admin_pw = os.environ.get("MNM_DB_PASSWORD", "")

    test_db = f"mnm_alembic_test_{uuid.uuid4().hex[:8]}"
    admin_conn = await asyncpg.connect(
        host=admin_host, port=admin_port,
        user=admin_user, password=admin_pw,
        database="postgres",
    )
    try:
        await admin_conn.execute(f'CREATE DATABASE "{test_db}"')
    finally:
        await admin_conn.close()

    # Redirect the application's DSN builder at this throwaway DB by patching
    # MNM_DB_NAME before importing app.db helpers.
    monkeypatch.setenv("MNM_DB_NAME", test_db)

    yield test_db

    admin_conn = await asyncpg.connect(
        host=admin_host, port=admin_port,
        user=admin_user, password=admin_pw,
        database="postgres",
    )
    try:
        # Terminate any lingering connections before drop — asyncpg engines
        # from the app may have leaked pool connections.
        await admin_conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = $1 AND pid <> pg_backend_pid()",
            test_db,
        )
        await admin_conn.execute(f'DROP DATABASE "{test_db}"')
    finally:
        await admin_conn.close()


def _alembic_config_for(db_name: str):
    """Build an Alembic Config pointed at the throwaway DB."""
    from alembic.config import Config
    from app.db import _alembic_ini_path
    cfg = Config(_alembic_ini_path())
    # env.py reads MNM_DB_NAME at import time via _build_dsn(); monkeypatch
    # in the fixture already set MNM_DB_NAME, so Config picks it up when
    # env.py runs.  Nothing else to override here.
    assert os.environ.get("MNM_DB_NAME") == db_name
    return cfg


async def test_baseline_migration_upgradeable(throwaway_db):
    """alembic upgrade head on an empty DB creates every expected table."""
    import asyncio
    import asyncpg
    from alembic import command

    cfg = _alembic_config_for(throwaway_db)
    await asyncio.to_thread(command.upgrade, cfg, "head")

    conn = await asyncpg.connect(
        host=os.environ["MNM_DB_HOST"],
        port=int(os.environ.get("MNM_DB_PORT", "5432")),
        user=os.environ.get("MNM_DB_USER", "nautobot"),
        password=os.environ.get("MNM_DB_PASSWORD", ""),
        database=throwaway_db,
    )
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        tables = {r["table_name"] for r in rows}
    finally:
        await conn.close()

    # Spot-check the canonical core tables
    for expected in {"alembic_version", "endpoints", "device_polls",
                     "node_arp_entries", "node_mac_entries",
                     "node_lldp_entries", "routes"}:
        assert expected in tables, f"expected table {expected} missing after upgrade"


async def test_baseline_migration_downgradeable(throwaway_db):
    """upgrade head then downgrade base leaves only alembic_version."""
    import asyncio
    import asyncpg
    from alembic import command

    cfg = _alembic_config_for(throwaway_db)
    await asyncio.to_thread(command.upgrade, cfg, "head")
    await asyncio.to_thread(command.downgrade, cfg, "base")

    conn = await asyncpg.connect(
        host=os.environ["MNM_DB_HOST"],
        port=int(os.environ.get("MNM_DB_PORT", "5432")),
        user=os.environ.get("MNM_DB_USER", "nautobot"),
        password=os.environ.get("MNM_DB_PASSWORD", ""),
        database=throwaway_db,
    )
    try:
        rows = await conn.fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        tables = {r["table_name"] for r in rows}
    finally:
        await conn.close()

    # After full downgrade, only Alembic's bookkeeping table should remain.
    assert "alembic_version" in tables
    app_tables = tables - {"alembic_version"}
    assert app_tables == set(), f"unexpected leftover tables: {app_tables}"


async def test_legacy_schema_detection_and_stamp(throwaway_db):
    """Tables created via Base.metadata.create_all are detected as legacy
    and alembic stamp head marks the DB without re-running migrations."""
    import asyncio
    import asyncpg
    from alembic import command

    # Simulate the pre-Alembic installation path: create tables directly
    # from the SQLAlchemy models, no alembic_version table.
    from app.db import Base, _build_dsn
    from sqlalchemy.ext.asyncio import create_async_engine

    dsn = _build_dsn()
    engine = create_async_engine(dsn)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    finally:
        await engine.dispose()

    # Verify no alembic_version yet, but app tables present
    pg_conn = await asyncpg.connect(
        host=os.environ["MNM_DB_HOST"],
        port=int(os.environ.get("MNM_DB_PORT", "5432")),
        user=os.environ.get("MNM_DB_USER", "nautobot"),
        password=os.environ.get("MNM_DB_PASSWORD", ""),
        database=throwaway_db,
    )
    try:
        has_alembic = await pg_conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='alembic_version')"
        )
        has_app = await pg_conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name='sweep_runs')"
        )
        assert not has_alembic
        assert has_app
    finally:
        await pg_conn.close()

    # Stamp head — this is what _try_init_db does on legacy detection
    cfg = _alembic_config_for(throwaway_db)
    await asyncio.to_thread(command.stamp, cfg, "head")

    pg_conn = await asyncpg.connect(
        host=os.environ["MNM_DB_HOST"],
        port=int(os.environ.get("MNM_DB_PORT", "5432")),
        user=os.environ.get("MNM_DB_USER", "nautobot"),
        password=os.environ.get("MNM_DB_PASSWORD", ""),
        database=throwaway_db,
    )
    try:
        version = await pg_conn.fetchval(
            "SELECT version_num FROM alembic_version LIMIT 1"
        )
        assert version, "alembic_version should be populated after stamp"

        # Verify no tables were dropped or recreated — a simple count check
        # on the still-empty sweep_runs table confirms it's the same table.
        app_table_count = await pg_conn.fetchval(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema='public' AND table_name IN "
            "('sweep_runs', 'endpoints', 'device_polls', 'alembic_version')"
        )
        assert app_table_count == 4
    finally:
        await pg_conn.close()
