"""Controller-side writer to the mnm-plugin Nautobot DB.

Per E0 §5c: the controller maintains a separate SQLAlchemy
connection pool to Nautobot's PostgreSQL database. Plugin
table layouts are read via reflection at first use — the
controller does NOT import Django.

Per E0 §5d (two-tier write): callers continue to write to the
controller DB via :mod:`endpoint_store` (authoritative for v1.0
operational pages). This module's writes are the **mirror** —
non-authoritative; failures log a warning and never break the
polling cycle. The Block C P3/P4/P5 lesson on adapter-level
dedup keyed on the unique constraint applies here too.

E1 ships :func:`upsert_endpoint_bulk`. E2 adds
:func:`upsert_arp_bulk`, :func:`upsert_mac_bulk`,
:func:`upsert_lldp_bulk` for the link-layer triad.

Connection details come from the existing ``NAUTOBOT_DB_*`` env
vars. The engine is constructed lazily so importing this
module in environments where Nautobot's DB is unreachable
doesn't fail.
"""

from __future__ import annotations

import os
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import MetaData, Table
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
_reflected_tables: dict[str, Table] = {}
_reflection_failed_once: dict[str, bool] = {}


def _build_dsn() -> str:
    """Build Nautobot's Postgres DSN from env.

    The controller's existing ``MNM_DB_*`` connection (see
    ``app.db._build_dsn``) targets the ``mnm_controller`` database
    on the shared Postgres instance. Plugin tables live in the
    ``nautobot`` database on the SAME instance with the SAME
    credentials — only the database name differs. So we reuse
    ``MNM_DB_HOST`` / ``MNM_DB_PORT`` / ``MNM_DB_USER`` /
    ``MNM_DB_PASSWORD`` from the controller env and override the
    database name to ``nautobot`` (operator-overridable via
    ``MNM_PLUGIN_DB_NAME``).
    """
    host = os.environ.get("MNM_DB_HOST", "postgres")
    port = os.environ.get("MNM_DB_PORT", "5432")
    name = os.environ.get("MNM_PLUGIN_DB_NAME", "nautobot")
    user = os.environ.get("MNM_DB_USER", "nautobot")
    pw = os.environ.get("MNM_DB_PASSWORD", "")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{name}"


def _ensure_engine() -> Optional[AsyncEngine]:
    """Lazily construct the engine. Return None if construction
    fails (e.g., env vars unset in a test context)."""
    global _engine, _session_maker, _metadata
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
        _metadata = MetaData()
        return _engine
    except Exception as e:  # noqa: BLE001
        log.warning(
            "plugin_engine_init_failed",
            "Could not construct plugin DB engine — plugin writes "
            "will be skipped this cycle",
            context={"error": str(e), "error_class": type(e).__name__},
        )
        return None


async def _ensure_table(table_name: str) -> Optional[Table]:
    """Lazily reflect ``table_name`` from Nautobot's DB.

    Caches the reflected ``Table`` keyed by name. On the first
    failure logs a warning; subsequent attempts for that name
    are silent until process restart. Returns ``None`` if the
    table doesn't exist (plugin not yet migrated) or the engine
    is unavailable.
    """
    if table_name in _reflected_tables:
        return _reflected_tables[table_name]

    engine = _ensure_engine()
    if engine is None or _metadata is None:
        return None

    try:
        async with engine.connect() as conn:
            await conn.run_sync(
                _metadata.reflect,
                only=[table_name],
            )
        table = _metadata.tables.get(table_name)
        if table is None:
            if not _reflection_failed_once.get(table_name):
                log.warning(
                    "plugin_table_missing",
                    "Plugin table not found in Nautobot DB. "
                    "Plugin migrations may not have run yet — plugin "
                    "writes will be skipped until the table exists.",
                    context={"table": table_name},
                )
                _reflection_failed_once[table_name] = True
            return None
        _reflected_tables[table_name] = table
        return table
    except (OperationalError, SQLAlchemyError) as e:
        if not _reflection_failed_once.get(table_name):
            log.warning(
                "plugin_reflection_failed",
                "Failed to reflect plugin table — plugin writes "
                "will be skipped this cycle.",
                context={
                    "table": table_name,
                    "error": str(e),
                    "error_class": type(e).__name__,
                },
            )
            _reflection_failed_once[table_name] = True
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inject_django_defaults(rows: list[dict], table) -> list[dict]:
    """Supply Python-side defaults that SQLAlchemy reflection
    can't see.

    Django models declare ``id = UUIDField(default=uuid.uuid4)``
    and ChangeLoggedModel adds ``created`` /
    ``last_updated`` with ``auto_now_add`` / ``auto_now``. None
    of those defaults survive ``MetaData.reflect`` — the columns
    arrive as NOT NULL with no default expression. We inject
    matching values per row so the INSERT statement provides
    everything PostgreSQL requires.

    Idempotent: existing values in the row are preserved.
    """
    has_id = "id" in table.c
    has_created = "created" in table.c
    has_last_updated = "last_updated" in table.c
    out = []
    for r in rows:
        merged = dict(r)
        if has_id and "id" not in merged:
            merged["id"] = uuid.uuid4()
        if has_created and "created" not in merged:
            merged["created"] = _utcnow()
        if has_last_updated and "last_updated" not in merged:
            merged["last_updated"] = _utcnow()
        out.append(merged)
    return out


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


def _coerce_dt(value) -> Optional[datetime]:
    """Coerce ISO-format strings or datetime instances to datetime.

    The endpoint correlator (``endpoint_collector._correlate_endpoints``)
    emits ``first_seen`` / ``last_seen`` as ISO-8601 strings; asyncpg
    rejects strings for ``DateTimeField`` columns. Accept both forms
    here, return ``None`` for empty / unparseable values so the
    caller's ``or _utcnow()`` fallback applies.
    """
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


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
        "dhcp_lease_start": _coerce_dt(ep.get("dhcp_lease_start")),
        "dhcp_lease_expiry": _coerce_dt(ep.get("dhcp_lease_expiry")),
        "first_seen": _coerce_dt(ep.get("first_seen")) or now,
        "last_seen": _coerce_dt(ep.get("last_seen")) or now,
        "data_source": ep.get("source") or ep.get("data_source") or "infrastructure",
    }


# ---------------------------------------------------------------------------
# Endpoint (E1)
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

    table = await _ensure_table("mnm_plugin_endpoint")
    if table is None:
        return 0

    rows = [_normalize_endpoint_dict(ep) for ep in endpoints]
    rows = [r for r in rows if r["mac_address"]]
    deduped = _dedup_by_constraint(rows, ENDPOINT_CONSTRAINT_KEYS)
    dedup_dropped = len(rows) - len(deduped)
    if not deduped:
        return 0

    # Inject Django-side defaults (id, created, last_updated)
    # that SQLAlchemy reflection can't see.
    deduped = _inject_django_defaults(deduped, table)

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


# ---------------------------------------------------------------------------
# ARP (E2)
# ---------------------------------------------------------------------------


ARP_CONSTRAINT_KEYS = ("node_name", "ip", "mac", "vrf")

# Columns updated when an ARP row already exists for the
# (node_name, ip, mac, vrf) tuple. interface may move (Junos
# ages out a stale entry into a new ifIndex), and collected_at
# is always refreshed.
_ARP_UPDATE_COLUMNS = ("interface", "collected_at")


def _normalize_arp_dict(node_name: str, e: dict) -> dict | None:
    """Map a polling.collect_arp output dict to a plugin row.

    Polling produces ``{ip, mac, interface}`` (no vrf — defaults
    to "default"). Returns ``None`` if the row lacks required
    fields.
    """
    ip = e.get("ip")
    mac = (e.get("mac") or "").upper()
    if not ip or not mac:
        return None
    return {
        "node_name": node_name,
        "ip": ip,
        "mac": mac,
        "interface": e.get("interface") or "",
        "vrf": e.get("vrf") or "default",
        "collected_at": _utcnow(),
    }


async def upsert_arp_bulk(node_name: str, entries: list[dict]) -> int:
    """Mirror ARP upserts into the plugin DB.

    Input shape matches what ``polling.collect_arp`` produces.
    ``collected_at`` is set by the writer (server-side, not
    caller-supplied) so all rows in a single cycle land with one
    consistent timestamp. Fail-soft per E1 patterns.
    """
    return await _upsert_bulk(
        table_name="mnm_plugin_arpentry",
        constraint_keys=ARP_CONSTRAINT_KEYS,
        update_columns=_ARP_UPDATE_COLUMNS,
        rows=[r for r in (_normalize_arp_dict(node_name, e) for e in entries) if r],
        log_event_prefix="plugin_arp_upsert",
    )


# ---------------------------------------------------------------------------
# MAC (E2)
# ---------------------------------------------------------------------------


MAC_CONSTRAINT_KEYS = ("node_name", "mac", "interface", "vlan")

_MAC_UPDATE_COLUMNS = ("entry_type", "collected_at")


def _normalize_mac_dict(node_name: str, e: dict) -> dict | None:
    """Map a polling.collect_mac output dict to a plugin row.

    Polling produces ``{mac, interface, vlan, static (bool)}``;
    the plugin schema's ``entry_type`` is ``"static"`` or
    ``"dynamic"``. Block C P4 entry_status remap stays in the
    polling adapter; we just translate the bool.
    """
    mac = (e.get("mac") or "").upper()
    if not mac:
        return None
    try:
        vlan = int(e.get("vlan") or 0)
    except (TypeError, ValueError):
        vlan = 0
    return {
        "node_name": node_name,
        "mac": mac,
        "interface": e.get("interface") or "",
        "vlan": vlan,
        "entry_type": "static" if e.get("static") else "dynamic",
        "collected_at": _utcnow(),
    }


async def upsert_mac_bulk(node_name: str, entries: list[dict]) -> int:
    """Mirror MAC/FDB upserts into the plugin DB.

    Input shape matches ``polling.collect_mac``. ``static``
    boolean is remapped to ``entry_type`` string. Fail-soft.
    """
    return await _upsert_bulk(
        table_name="mnm_plugin_macentry",
        constraint_keys=MAC_CONSTRAINT_KEYS,
        update_columns=_MAC_UPDATE_COLUMNS,
        rows=[r for r in (_normalize_mac_dict(node_name, e) for e in entries) if r],
        log_event_prefix="plugin_mac_upsert",
    )


# ---------------------------------------------------------------------------
# LLDP (E2)
# ---------------------------------------------------------------------------


LLDP_CONSTRAINT_KEYS = (
    "node_name",
    "local_interface",
    "remote_system_name",
    "remote_port",
)

_LLDP_UPDATE_COLUMNS = (
    "remote_chassis_id",
    "remote_management_ip",
    "local_port_ifindex",
    "local_port_name",
    "remote_chassis_id_subtype",
    "remote_port_id_subtype",
    "remote_system_description",
    "collected_at",
)


def _normalize_lldp_neighbor(
    node_name: str, local_iface: str, n: dict,
) -> dict:
    """Map a polling-side LLDP neighbor dict to a plugin row.

    polling.collect_lldp produces a grouped dict
    ``{local_iface: [neighbor, ...]}``; each neighbor is a flat
    dict with the NAPALM-shape fields plus the five Block C P2
    expansion columns. We keep those names verbatim and add
    ``node_name`` + ``local_interface`` + ``collected_at``.
    """
    return {
        "node_name": node_name,
        "local_interface": local_iface,
        "remote_system_name": n.get("remote_system_name") or "",
        "remote_port": n.get("remote_port") or "",
        "remote_chassis_id": n.get("remote_chassis_id") or None,
        "remote_management_ip": n.get("remote_management_ip") or None,
        "local_port_ifindex": n.get("local_port_ifindex"),
        "local_port_name": n.get("local_port_name") or None,
        "remote_chassis_id_subtype": n.get("remote_chassis_id_subtype") or None,
        "remote_port_id_subtype": n.get("remote_port_id_subtype") or None,
        "remote_system_description": n.get("remote_system_description") or None,
        "collected_at": _utcnow(),
    }


async def upsert_lldp_bulk(node_name: str, grouped: dict) -> int:
    """Mirror LLDP upserts into the plugin DB.

    Input shape is the grouped dict ``{local_iface: [neighbor,
    ...]}`` that ``polling.collect_lldp`` already produces (per
    the P5 lesson). The plugin schema is flat (one row per
    neighbor with ``local_interface`` denormalized), so we
    iterate-and-flatten before upsert.
    """
    rows: list[dict] = []
    for local_iface, neighbors in (grouped or {}).items():
        for n in neighbors or []:
            rows.append(_normalize_lldp_neighbor(node_name, local_iface, n))
    return await _upsert_bulk(
        table_name="mnm_plugin_lldpneighbor",
        constraint_keys=LLDP_CONSTRAINT_KEYS,
        update_columns=_LLDP_UPDATE_COLUMNS,
        rows=rows,
        log_event_prefix="plugin_lldp_upsert",
    )


# ---------------------------------------------------------------------------
# Internal: shared upsert plumbing
# ---------------------------------------------------------------------------


async def _upsert_bulk(
    *,
    table_name: str,
    constraint_keys: tuple[str, ...],
    update_columns: tuple[str, ...],
    rows: list[dict],
    log_event_prefix: str,
) -> int:
    """Shared INSERT...ON CONFLICT DO UPDATE for E2 link-layer
    tables.

    Adapter-level dedup on ``constraint_keys``, batched
    ``ON CONFLICT DO UPDATE`` setting ``update_columns``, BLE001
    guard. Returns the count attempted (after dedup), 0 on any
    error path.
    """
    if not rows:
        return 0

    table = await _ensure_table(table_name)
    if table is None:
        return 0

    deduped = _dedup_by_constraint(rows, constraint_keys)
    dedup_dropped = len(rows) - len(deduped)
    if not deduped:
        return 0

    # Django-side defaults (uuid.uuid4 on the ``id`` PK,
    # ``timezone.now`` on ``created`` / ``last_updated``) are
    # invisible to SQLAlchemy reflection — supply them
    # explicitly so the INSERT doesn't NULL-out NOT-NULL columns.
    deduped = _inject_django_defaults(deduped, table)

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
            stmt = stmt.on_conflict_do_update(
                index_elements=list(constraint_keys),
                set_=update_set,
            )
            await session.execute(stmt)
            await session.commit()

        log.info(
            log_event_prefix,
            "Upserted plugin rows",
            context={
                "table": table_name,
                "count": len(deduped),
                "dedup_dropped": dedup_dropped,
            },
        )
        return len(deduped)

    except IntegrityError as e:
        log.warning(
            f"{log_event_prefix}_integrity_error",
            "Integrity error during plugin upsert "
            "(non-fatal — controller DB is authoritative)",
            context={
                "table": table_name,
                "error": str(e),
                "count": len(deduped),
            },
        )
        return 0
    except Exception as e:  # noqa: BLE001
        log.warning(
            f"{log_event_prefix}_failed",
            "Plugin upsert failed (non-fatal — controller DB is "
            "authoritative)",
            context={
                "table": table_name,
                "error": str(e),
                "error_class": type(e).__name__,
                "count": len(deduped),
            },
        )
        return 0


__all__ = [
    "ENDPOINT_CONSTRAINT_KEYS",
    "ARP_CONSTRAINT_KEYS",
    "MAC_CONSTRAINT_KEYS",
    "LLDP_CONSTRAINT_KEYS",
    "_dedup_by_constraint",
    "_normalize_endpoint_dict",
    "_normalize_arp_dict",
    "_normalize_mac_dict",
    "_normalize_lldp_neighbor",
    "upsert_endpoint_bulk",
    "upsert_arp_bulk",
    "upsert_mac_bulk",
    "upsert_lldp_bulk",
]
