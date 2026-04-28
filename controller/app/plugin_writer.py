"""Controller-side writer to the mnm-plugin Nautobot DB.

Per E0 §5c: the controller maintains a separate SQLAlchemy
connection pool to Nautobot's PostgreSQL database. The plugin's
table layout is read via reflection at first use — the controller
does NOT import Django.

Per E0 §5d (two-tier write): callers continue to write to the
controller DB via :mod:`endpoint_store` (authoritative for v1.0
operational pages). This module's writes are the **mirror** —
non-authoritative; failures log a warning and never break the
polling cycle. The Block C P3/P4/P5 lesson on adapter-level dedup
keyed on the unique constraint applies here too.

Connection details come from the existing ``NAUTOBOT_DB_*`` env
vars. The engine is constructed lazily so importing this module
in environments where Nautobot's DB is unreachable doesn't fail.
"""

from __future__ import annotations

import os
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from sqlalchemy import MetaData, Table, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="plugin_writer")


# ---------------------------------------------------------------------------
# Engine / session — lazy singleton
# ---------------------------------------------------------------------------

_engine: Optional[AsyncEngine] = None
_session_maker = None
_metadata: Optional[MetaData] = None
_endpoint_table: Optional[Table] = None
_reflection_failed_once = False


def _build_dsn() -> str:
    """Build Nautobot's Postgres DSN from env."""
    host = os.environ.get("NAUTOBOT_DB_HOST", "postgres")
    port = os.environ.get("NAUTOBOT_DB_PORT", "5432")
    name = os.environ.get("NAUTOBOT_DB_NAME", "nautobot")
    user = os.environ.get("NAUTOBOT_DB_USER", "nautobot")
    pw = os.environ.get("NAUTOBOT_DB_PASSWORD", "")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{name}"


def _ensure_engine() -> Optional[AsyncEngine]:
    """Lazily construct the engine. Return None if construction
    fails (e.g., env vars unset in a test context)."""
    global _engine, _session_maker
    if _engine is not None:
        return _engine
    try:
        pool_size = int(os.environ.get("MNM_PLUGIN_DB_POOL_SIZE", "5"))
        _engine = create_async_engine(
            _build_dsn(),
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=2,
        )
        _session_maker = async_sessionmaker(
            _engine, expire_on_commit=False,
        )
        return _engine
    except Exception as e:  # noqa: BLE001
        log.warning(
            "plugin_engine_init_failed",
            "Could not construct plugin DB engine — plugin writes "
            "will be skipped this cycle",
            context={"error": str(e), "error_class": type(e).__name__},
        )
        return None


async def _ensure_endpoint_table() -> Optional[Table]:
    """Lazily reflect the ``mnm_plugin_endpoint`` table layout.

    Returns the SQLAlchemy ``Table`` once successful; ``None`` if
    reflection fails (plugin not yet migrated, or Nautobot DB
    unreachable). On the first failure logs a warning; subsequent
    attempts are silent until the next process restart.
    """
    global _metadata, _endpoint_table, _reflection_failed_once
    if _endpoint_table is not None:
        return _endpoint_table

    engine = _ensure_engine()
    if engine is None:
        return None

    try:
        _metadata = MetaData()
        async with engine.connect() as conn:
            await conn.run_sync(
                _metadata.reflect,
                only=["mnm_plugin_endpoint"],
            )
        _endpoint_table = _metadata.tables.get("mnm_plugin_endpoint")
        if _endpoint_table is None and not _reflection_failed_once:
            log.warning(
                "plugin_table_missing",
                "mnm_plugin_endpoint table not found in Nautobot DB. "
                "Plugin migrations may not have run yet — plugin "
                "writes will be skipped until the table exists.",
            )
            _reflection_failed_once = True
        return _endpoint_table
    except (OperationalError, SQLAlchemyError) as e:
        if not _reflection_failed_once:
            log.warning(
                "plugin_reflection_failed",
                "Failed to reflect mnm_plugin schema — plugin "
                "writes will be skipped this cycle.",
                context={"error": str(e), "error_class": type(e).__name__},
            )
            _reflection_failed_once = True
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dedup_by_constraint(
    entries: Iterable[dict],
    constraint_keys: tuple[str, ...],
) -> list[dict]:
    """Deduplicate upsert dicts by their unique constraint key.

    Last entry wins (consistent with ``ON CONFLICT DO UPDATE``
    semantics). Required by Block C P3/P4/P5 lesson — Postgres
    raises ``CardinalityViolationError`` when an
    ``INSERT ... ON CONFLICT`` batch contains rows that collide
    on the conflict target. Dedup at the adapter prevents batch
    loss; the BLE001 guard at the upsert prevents task crash.
    """
    seen: "OrderedDict[tuple, dict]" = OrderedDict()
    for entry in entries:
        key = tuple(entry.get(k) for k in constraint_keys)
        seen[key] = entry
    return list(seen.values())


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_endpoint_dict(ep: dict) -> dict:
    """Map an endpoint dict from ``_correlate_endpoints`` shape to
    the plugin's ``Endpoint`` row shape.

    The correlator emits dicts with keys like ``mac``, ``ip``,
    ``device_name``, ``switch_port``, ``vlan``. The plugin model
    columns are ``mac_address``, ``current_switch``,
    ``current_port``, ``current_vlan``, ``current_ip``. Translate.

    Sentinel values (``"(none)"`` for switch/port, ``0`` for
    vlan) are preserved per E0 §2e.
    """
    mac = (ep.get("mac") or ep.get("mac_address") or "").upper()
    switch = ep.get("device_name") or ep.get("current_switch") or "(none)"
    port = ep.get("switch_port") or ep.get("current_port") or "(none)"
    vlan_raw = ep.get("vlan") or ep.get("current_vlan") or 0
    try:
        vlan = int(vlan_raw) if vlan_raw not in (None, "") else 0
    except (TypeError, ValueError):
        vlan = 0
    ip = ep.get("ip") or ep.get("current_ip") or None
    additional_ips = list(ep.get("additional_ips") or [])
    if ip and ip not in additional_ips:
        additional_ips.append(ip)
    now = _utcnow()
    return {
        "mac_address": mac,
        "current_switch": switch,
        "current_port": port,
        "current_vlan": vlan,
        "current_ip": ip,
        "additional_ips": additional_ips,
        "mac_vendor": ep.get("mac_vendor") or None,
        "hostname": ep.get("hostname") or ep.get("dhcp_hostname") or None,
        "classification": ep.get("classification") or None,
        "classification_confidence": ep.get("classification_confidence") or None,
        "classification_override": bool(ep.get("classification_override", False)),
        "active": bool(ep.get("active", True)),
        "is_uplink": bool(ep.get("is_uplink", False)),
        "dhcp_server": ep.get("dhcp_server") or None,
        "dhcp_lease_start": ep.get("dhcp_lease_start") or None,
        "dhcp_lease_expiry": ep.get("dhcp_lease_expiry") or None,
        "first_seen": ep.get("first_seen") or now,
        "last_seen": ep.get("last_seen") or now,
        "data_source": ep.get("source") or ep.get("data_source") or "infrastructure",
    }


# ---------------------------------------------------------------------------
# Public API — bulk upserts
# ---------------------------------------------------------------------------


ENDPOINT_CONSTRAINT_KEYS = (
    "mac_address",
    "current_switch",
    "current_port",
    "current_vlan",
)


async def upsert_endpoint_bulk(endpoints: list[dict]) -> int:
    """Upsert Endpoint rows to the plugin DB.

    - Translates correlator-shape dicts to plugin column names.
    - Adapter-level dedup on
      ``(mac_address, current_switch, current_port, current_vlan)``.
    - Single batched ``INSERT ... ON CONFLICT DO UPDATE``.
    - Failures are logged and swallowed: the controller DB write
      is authoritative, plugin DB is the mirror, and the polling
      cycle continues regardless.

    Returns the number of rows successfully attempted (after
    dedup). Returns 0 on any error path.
    """
    if not endpoints:
        return 0

    table = await _ensure_endpoint_table()
    if table is None:
        return 0

    rows = [_normalize_endpoint_dict(ep) for ep in endpoints]
    rows = [r for r in rows if r["mac_address"]]
    deduped = _dedup_by_constraint(rows, ENDPOINT_CONSTRAINT_KEYS)
    dedup_dropped = len(rows) - len(deduped)
    if not deduped:
        return 0

    # Plugin's BaseModel + ChangeLoggedModel adds `id` (UUID),
    # `created`, `last_updated`. We let Postgres / Django defaults
    # handle them — the table reflection includes the columns,
    # but we don't supply values, so default expressions apply.
    # On UPDATE we only touch the plugin's domain columns plus
    # `last_updated`.

    update_columns = [
        "active",
        "is_uplink",
        "current_ip",
        "additional_ips",
        "mac_vendor",
        "hostname",
        "classification",
        "classification_confidence",
        "classification_override",
        "dhcp_server",
        "dhcp_lease_start",
        "dhcp_lease_expiry",
        "last_seen",
        "data_source",
    ]

    try:
        if _session_maker is None:
            return 0
        async with _session_maker() as session:
            stmt = insert(table).values(deduped)
            update_set = {
                col: getattr(stmt.excluded, col)
                for col in update_columns
                if col in table.c
            }
            # Update last_updated if the column exists on the
            # reflected table (it does for ChangeLoggedModel rows).
            if "last_updated" in table.c:
                update_set["last_updated"] = _utcnow()

            stmt = stmt.on_conflict_do_update(
                index_elements=list(ENDPOINT_CONSTRAINT_KEYS),
                set_=update_set,
            )
            await session.execute(stmt)
            await session.commit()

        log.info(
            "plugin_endpoint_upsert",
            "Upserted endpoints into plugin DB",
            context={
                "count": len(deduped),
                "dedup_dropped": dedup_dropped,
            },
        )
        return len(deduped)

    except IntegrityError as e:
        log.warning(
            "plugin_endpoint_upsert_integrity_error",
            "Integrity error during plugin endpoint upsert "
            "(non-fatal — controller DB is authoritative)",
            context={"error": str(e), "count": len(deduped)},
        )
        return 0
    except Exception as e:  # noqa: BLE001
        # BLE001 guard per Block C P5 lesson —
        # CardinalityViolationError and other unexpected
        # exceptions land here. Plugin writes are non-
        # authoritative; never fail the polling cycle.
        log.warning(
            "plugin_endpoint_upsert_failed",
            "Plugin endpoint upsert failed (non-fatal — controller "
            "DB is authoritative)",
            context={
                "error": str(e),
                "error_class": type(e).__name__,
                "count": len(deduped),
            },
        )
        return 0


__all__ = [
    "ENDPOINT_CONSTRAINT_KEYS",
    "_dedup_by_constraint",
    "_normalize_endpoint_dict",
    "upsert_endpoint_bulk",
]
