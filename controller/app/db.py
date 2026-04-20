"""PostgreSQL persistence for MNM Controller (Phase 2.7).

Provides SQLAlchemy async models for the controller's local database — separate
from Nautobot's database, hosted on the same `mnm-postgres` instance under the
`mnm_controller` database.

Tables:
  - endpoints           — current MAC-keyed identity records
  - endpoint_events     — append-only log of changes (movement, ip, hostname)
  - sweep_runs          — per-sweep summaries
  - collection_runs     — per-collection-run summaries
  - ip_observations     — sweep-time per-IP snapshots (Shodan-style)
  - routes              — per-node routing table snapshots
  - bgp_neighbors       — per-node BGP peer state
  - auto_discovery_runs — hop-limited LLDP auto-discovery run logs
  - kv_config           — controller config (replaces config.json)

Nautobot IPAM remains the source of truth for IP records. This database tracks
temporal/event data that Nautobot's model doesn't natively support.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

from sqlalchemy import (
    Boolean, Column, DateTime, Float, Integer, PrimaryKeyConstraint, Text,
    UniqueConstraint, select,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.ext.asyncio import (
    AsyncSession, async_sessionmaker, create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="db")


# ---------------------------------------------------------------------------
# Engine / session
# ---------------------------------------------------------------------------

def _build_dsn() -> str:
    host = os.environ.get("MNM_DB_HOST", "postgres")
    port = os.environ.get("MNM_DB_PORT", "5432")
    name = os.environ.get("MNM_DB_NAME", "mnm_controller")
    user = os.environ.get("MNM_DB_USER", "nautobot")
    pw = os.environ.get("MNM_DB_PASSWORD", "")
    return f"postgresql+asyncpg://{user}:{pw}@{host}:{port}/{name}"


DSN = _build_dsn()
engine = create_async_engine(DSN, pool_pre_ping=True, pool_size=10, max_overflow=20)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

# How long to wait for the DB before giving up and falling back to JSON mode.
# On cold start the controller races Nautobot's bootstrap; 5 minutes is enough
# for bootstrap to create the mnm_controller database.
DB_INIT_MAX_WAIT_SEC = 300
DB_INIT_RETRY_INTERVAL_SEC = 5


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _uuid() -> uuid.UUID:
    return uuid.uuid4()


class Endpoint(Base):
    """MAC-on-port identity. Composite key: (mac, switch, port, vlan).

    A single MAC may have multiple rows when it has been seen on more than one
    (switch, port, vlan) combination. The most recent location is the row with
    `active=true`; all prior locations remain in the table as inactive history.
    """
    __tablename__ = "endpoints"

    mac_address = Column(Text, primary_key=True)
    current_switch = Column(Text, primary_key=True)
    current_port = Column(Text, primary_key=True)
    current_vlan = Column(Integer, primary_key=True)

    active = Column(Boolean, nullable=False, default=True, index=True)
    is_uplink = Column(Boolean, nullable=False, default=False)

    current_ip = Column(Text)
    # All IPs ever observed for this MAC at this (switch, port, vlan).
    # Stored as a JSON array of strings; current_ip is whichever one was most
    # recently asserted as primary. Multi-homed VMs and dual-stack hosts both
    # populate this naturally.
    additional_ips = Column(JSONB, nullable=False, default=list)
    mac_vendor = Column(Text)
    hostname = Column(Text)  # best available: DHCP > DNS > SNMP sysName > LLDP
    classification = Column(Text)
    classification_confidence = Column(Text)  # high, medium, low
    classification_override = Column(Boolean, nullable=False, default=False)
    dhcp_server = Column(Text)
    dhcp_lease_start = Column(DateTime(timezone=True))
    dhcp_lease_expiry = Column(DateTime(timezone=True))
    first_seen = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    last_seen = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    data_source = Column(Text)  # sweep, infrastructure, both
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)

    __table_args__ = (
        PrimaryKeyConstraint("mac_address", "current_switch", "current_port", "current_vlan",
                              name="pk_endpoints_mac_loc"),
    )

    def to_dict(self) -> dict:
        return {
            "mac": self.mac_address,
            "mac_address": self.mac_address,
            "ip": self.current_ip,
            "current_ip": self.current_ip,
            "additional_ips": list(self.additional_ips or []),
            "all_ips": ([self.current_ip] if self.current_ip else []) + [
                ip for ip in (self.additional_ips or []) if ip and ip != self.current_ip
            ],
            "mac_vendor": self.mac_vendor or "",
            "hostname": self.hostname or "",
            "classification": self.classification or "",
            "classification_confidence": self.classification_confidence or "",
            "classification_override": bool(self.classification_override),
            "device_name": self.current_switch or "",
            "current_switch": self.current_switch or "",
            "switch_port": self.current_port or "",
            "current_port": self.current_port or "",
            "vlan": self.current_vlan or 0,
            "current_vlan": self.current_vlan,
            "active": bool(self.active),
            "is_uplink": bool(self.is_uplink),
            "dhcp_server": self.dhcp_server or "",
            "lease_start": self.dhcp_lease_start.isoformat() if self.dhcp_lease_start else "",
            "lease_expiry": self.dhcp_lease_expiry.isoformat() if self.dhcp_lease_expiry else "",
            "first_seen": self.first_seen.isoformat() if self.first_seen else "",
            "last_seen": self.last_seen.isoformat() if self.last_seen else "",
            "source": self.data_source or "",
        }


class EndpointEvent(Base):
    """Append-only event log: appearance, movement, IP/hostname change."""
    __tablename__ = "endpoint_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    mac_address = Column(Text, index=True, nullable=False)
    event_type = Column(Text, nullable=False, index=True)
    # appeared, disappeared, moved_port, moved_switch, ip_changed, hostname_changed
    old_value = Column(Text)
    new_value = Column(Text)
    details = Column(JSONB)
    timestamp = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "mac_address": self.mac_address,
            "event_type": self.event_type,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "details": self.details or {},
            "timestamp": self.timestamp.isoformat() if self.timestamp else "",
        }


class SweepRun(Base):
    __tablename__ = "sweep_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    cidr = Column(Text)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True))
    total_scanned = Column(Integer, default=0)
    total_alive = Column(Integer, default=0)
    total_onboarded = Column(Integer, default=0)
    total_failed = Column(Integer, default=0)
    duration_seconds = Column(Float)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "cidr": self.cidr,
            "cidr_ranges": [self.cidr] if self.cidr else [],
            "started_at": self.started_at.isoformat() if self.started_at else "",
            "finished_at": self.completed_at.isoformat() if self.completed_at else "",
            "duration_seconds": self.duration_seconds,
            "summary": {
                "total": self.total_scanned or 0,
                "alive": self.total_alive or 0,
                "onboarded": self.total_onboarded or 0,
                "failed": self.total_failed or 0,
            },
        }


class CollectionRun(Base):
    __tablename__ = "collection_runs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    completed_at = Column(DateTime(timezone=True))
    devices_queried = Column(Integer, default=0)
    endpoints_found = Column(Integer, default=0)
    endpoints_new = Column(Integer, default=0)
    endpoints_updated = Column(Integer, default=0)
    endpoints_moved = Column(Integer, default=0)
    duration_seconds = Column(Float)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "started_at": self.started_at.isoformat() if self.started_at else "",
            "finished_at": self.completed_at.isoformat() if self.completed_at else "",
            "duration_seconds": self.duration_seconds,
            "devices_queried": self.devices_queried or 0,
            "endpoints_found": self.endpoints_found or 0,
            "endpoints_new": self.endpoints_new or 0,
            "endpoints_updated": self.endpoints_updated or 0,
            "endpoints_moved": self.endpoints_moved or 0,
            # Compatibility shims for the old UI
            "endpoints_recorded": self.endpoints_found or 0,
            "record_failed": 0,
            "devices_failed": 0,
        }


class IPObservation(Base):
    """Per-sweep snapshot of an IP. Append-only — used for IP history queries."""
    __tablename__ = "ip_observations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    ip_address = Column(Text, index=True, nullable=False)
    mac_address = Column(Text, index=True)
    ports_open = Column(JSONB)
    banners = Column(JSONB)
    snmp_data = Column(JSONB)
    http_headers = Column(JSONB)
    tls_data = Column(JSONB)
    ssh_banner = Column(Text)
    classification = Column(Text)
    dns_name = Column(Text)
    observed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)


class EndpointWatch(Base):
    """Operator-defined watchlist of MACs to flag in event feeds."""
    __tablename__ = "endpoint_watches"

    mac_address = Column(Text, primary_key=True)
    reason = Column(Text)
    created_by = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "mac_address": self.mac_address,
            "reason": self.reason or "",
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class DiscoveryExclude(Base):
    """Operator-defined exclusion list for sweep + collection.

    One row per excluded thing. The ``identifier`` is either an IP address
    or a device name; the ``type`` discriminator says which:

      - ``type='ip'``        — sweep skips before probing, collector skips
                                ARP/MAC correlation for this IP.
      - ``type='device_name'`` — Incomplete Devices advisory hides any
                                Nautobot device with this exact name.

    Two types, one table, no overloading. Inviolable Rule 6 — operator
    owns scope.
    """
    __tablename__ = "discovery_excludes"

    identifier = Column(Text, primary_key=True)
    type = Column(Text, nullable=False)  # 'ip' or 'device_name'
    reason = Column(Text)
    created_by = Column(Text)
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False)

    def to_dict(self) -> dict:
        return {
            "identifier": self.identifier,
            "type": self.type,
            "reason": self.reason or "",
            "created_by": self.created_by or "",
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class DevicePoll(Base):
    """Per-device, per-job-type poll tracking.

    Tracks when each collection job type (arp, mac, dhcp, lldp) last ran
    against each device, whether it succeeded, when it's next due, and
    the operator's per-device interval override.
    """
    __tablename__ = "device_polls"

    device_name = Column(Text, primary_key=True)
    job_type = Column(Text, primary_key=True)  # 'arp', 'mac', 'dhcp', 'lldp'
    last_success = Column(DateTime(timezone=True))
    last_attempt = Column(DateTime(timezone=True))
    last_error = Column(Text)
    last_duration = Column(Float)
    next_due = Column(DateTime(timezone=True))
    interval_sec = Column(Integer, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)

    __table_args__ = (
        PrimaryKeyConstraint("device_name", "job_type", name="pk_device_polls"),
    )

    def to_dict(self) -> dict:
        return {
            "device_name": self.device_name,
            "job_type": self.job_type,
            "last_success": self.last_success.isoformat() if self.last_success else None,
            "last_attempt": self.last_attempt.isoformat() if self.last_attempt else None,
            "last_error": self.last_error,
            "last_duration": self.last_duration,
            "next_due": self.next_due.isoformat() if self.next_due else None,
            "interval_sec": self.interval_sec,
            "enabled": self.enabled,
        }


class Route(Base):
    """Per-node routing table entry collected via NAPALM.

    Rows are upserted each collection cycle. Old rows persist with their
    collected_at timestamp and are cleaned by the daily prune job.
    """
    __tablename__ = "routes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(Text, nullable=False, index=True)
    prefix = Column(Text, nullable=False, index=True)
    next_hop = Column(Text, nullable=False, default="")
    protocol = Column(Text, nullable=False, default="unknown")
    vrf = Column(Text, nullable=False, default="default")
    metric = Column(Integer)
    preference = Column(Integer)  # administrative distance
    outgoing_interface = Column(Text)
    active = Column(Boolean, nullable=False, default=True)
    collected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("node_name", "prefix", "next_hop", "vrf",
                         name="uq_routes_node_prefix_nh_vrf"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "node_name": self.node_name,
            "prefix": self.prefix,
            "next_hop": self.next_hop or "",
            "protocol": self.protocol or "unknown",
            "vrf": self.vrf or "default",
            "metric": self.metric,
            "preference": self.preference,
            "outgoing_interface": self.outgoing_interface or "",
            "active": bool(self.active),
            "collected_at": self.collected_at.isoformat() if self.collected_at else "",
        }


class BGPNeighbor(Base):
    """Per-node BGP neighbor state collected via NAPALM.

    Rows are upserted each collection cycle. Old rows persist with their
    collected_at timestamp and are cleaned by the daily prune job.
    """
    __tablename__ = "bgp_neighbors"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(Text, nullable=False, index=True)
    neighbor_ip = Column(Text, nullable=False)
    remote_asn = Column(Integer, nullable=False)
    local_asn = Column(Integer)
    state = Column(Text, nullable=False, default="Unknown")
    prefixes_received = Column(Integer)
    prefixes_sent = Column(Integer)
    uptime_seconds = Column(Integer)
    vrf = Column(Text, nullable=False, default="default")
    address_family = Column(Text, nullable=False, default="ipv4 unicast")
    collected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("node_name", "neighbor_ip", "vrf", "address_family",
                         name="uq_bgp_node_neighbor_vrf_af"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "node_name": self.node_name,
            "neighbor_ip": self.neighbor_ip,
            "remote_asn": self.remote_asn,
            "local_asn": self.local_asn,
            "state": self.state or "Unknown",
            "prefixes_received": self.prefixes_received,
            "prefixes_sent": self.prefixes_sent,
            "uptime_seconds": self.uptime_seconds,
            "vrf": self.vrf or "default",
            "address_family": self.address_family or "ipv4 unicast",
            "collected_at": self.collected_at.isoformat() if self.collected_at else "",
        }


class NodeArpEntry(Base):
    """Per-node ARP table snapshot. Upserted each poll cycle."""
    __tablename__ = "node_arp_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(Text, nullable=False, index=True)
    ip = Column(Text, nullable=False)
    mac = Column(Text, nullable=False)
    interface = Column(Text, nullable=False, default="")
    vrf = Column(Text, nullable=False, default="default")
    collected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("node_name", "ip", "mac", "vrf",
                         name="uq_arp_node_ip_mac_vrf"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "node_name": self.node_name, "ip": self.ip,
            "mac": self.mac, "interface": self.interface,
            "vrf": self.vrf or "default",
            "collected_at": self.collected_at.isoformat() if self.collected_at else "",
        }


class NodeMacEntry(Base):
    """Per-node MAC/FDB table snapshot. Upserted each poll cycle."""
    __tablename__ = "node_mac_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(Text, nullable=False, index=True)
    mac = Column(Text, nullable=False)
    interface = Column(Text, nullable=False, default="")
    vlan = Column(Integer, nullable=False, default=0)
    entry_type = Column(Text, nullable=False, default="dynamic")
    collected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("node_name", "mac", "interface", "vlan",
                         name="uq_mac_node_mac_iface_vlan"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "node_name": self.node_name, "mac": self.mac,
            "interface": self.interface, "vlan": self.vlan,
            "entry_type": self.entry_type or "dynamic",
            "collected_at": self.collected_at.isoformat() if self.collected_at else "",
        }


class NodeLldpEntry(Base):
    """Per-node LLDP neighbor snapshot. Upserted each poll cycle."""
    __tablename__ = "node_lldp_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(Text, nullable=False, index=True)
    local_interface = Column(Text, nullable=False)
    remote_system_name = Column(Text, nullable=False, default="")
    remote_port = Column(Text, nullable=False, default="")
    remote_chassis_id = Column(Text)
    remote_management_ip = Column(Text)
    collected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("node_name", "local_interface", "remote_system_name", "remote_port",
                         name="uq_lldp_node_iface_remote"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "node_name": self.node_name,
            "local_interface": self.local_interface,
            "remote_system_name": self.remote_system_name or "",
            "remote_port": self.remote_port or "",
            "remote_chassis_id": self.remote_chassis_id or "",
            "remote_management_ip": self.remote_management_ip or "",
            "collected_at": self.collected_at.isoformat() if self.collected_at else "",
        }


class NodeFibEntry(Base):
    """Per-node forwarding table (FIB) snapshot.

    On most platforms, the SNMP routing MIB reflects the actual forwarding
    table. This table stores FIB data when a dedicated FIB source exists
    (e.g. Junos 'show route forwarding-table'). When no dedicated source
    is available, populated from the SNMP route collection as a best-effort
    approximation — marked with source='snmp_rib'.
    """
    __tablename__ = "node_fib_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    node_name = Column(Text, nullable=False, index=True)
    prefix = Column(Text, nullable=False)
    next_hop = Column(Text, nullable=False, default="")
    interface = Column(Text)
    vrf = Column(Text, nullable=False, default="default")
    source = Column(Text, nullable=False, default="snmp_rib")  # snmp_rib or cli_fib
    collected_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("node_name", "prefix", "next_hop", "vrf",
                         name="uq_fib_node_prefix_nh_vrf"),
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id, "node_name": self.node_name,
            "prefix": self.prefix, "next_hop": self.next_hop or "",
            "interface": self.interface or "", "vrf": self.vrf or "default",
            "source": self.source or "snmp_rib",
            "collected_at": self.collected_at.isoformat() if self.collected_at else "",
        }


class AutoDiscoveryRun(Base):
    """Persistent log of hop-limited auto-discovery runs.

    Each row records one auto_discover_from_node() invocation with its
    seed node, hop limit, results summary, and per-node detail in JSONB.
    """
    __tablename__ = "auto_discovery_runs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    seed_node = Column(Text, nullable=False, index=True)
    max_hops = Column(Integer, nullable=False)
    attempted = Column(Integer, nullable=False, default=0)
    succeeded = Column(Integer, nullable=False, default=0)
    failed = Column(Integer, nullable=False, default=0)
    skipped = Column(Integer, nullable=False, default=0)
    nodes = Column(JSONB, nullable=False, default=list)
    triggered_by = Column(Text, nullable=False, default="manual")  # "sweep" or "manual"
    started_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)
    finished_at = Column(DateTime(timezone=True))

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "seed_node": self.seed_node,
            "max_hops": self.max_hops,
            "attempted": self.attempted,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "nodes": self.nodes or [],
            "triggered_by": self.triggered_by or "manual",
            "started_at": self.started_at.isoformat() if self.started_at else "",
            "finished_at": self.finished_at.isoformat() if self.finished_at else "",
        }


class KVConfig(Base):
    """Simple key/value store that replaces config.json."""
    __tablename__ = "kv_config"

    key = Column(Text, primary_key=True)
    value = Column(JSONB, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=_utcnow, onupdate=_utcnow, nullable=False)


class Comment(Base):
    """Operator-created comments attached to endpoints or nodes.

    target_type is either "endpoint" (target_id = MAC address) or "node"
    (target_id = node name). Comments are immutable once created — edit =
    delete and re-add. Comments are NOT pruned; they persist until manually
    deleted by the operator.
    """
    __tablename__ = "comments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_type = Column(Text, nullable=False, index=True)
    target_id = Column(Text, nullable=False, index=True)
    comment_text = Column(Text, nullable=False)
    created_by = Column(Text, nullable=False, default="admin")
    created_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "target_type": self.target_type,
            "target_id": self.target_id,
            "comment_text": self.comment_text,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else "",
        }


class ChangeHistory(Base):
    """Field-level change log for endpoints and nodes.

    Records what changed, when, and what triggered it. Distinct from
    EndpointEvent (which tracks movement events like moved_port, ip_changed) —
    this is a broader audit trail including metadata updates, classification
    changes, and comment add/delete.

    change_source values: poll_arp, poll_mac, poll_dhcp, poll_lldp, poll_routes,
    poll_bgp, sweep, manual, comment, onboarding.
    """
    __tablename__ = "change_history"

    id = Column(UUID(as_uuid=True), primary_key=True, default=_uuid)
    target_type = Column(Text, nullable=False, index=True)
    target_id = Column(Text, nullable=False, index=True)
    field_name = Column(Text, nullable=False)
    old_value = Column(Text)
    new_value = Column(Text)
    change_source = Column(Text, nullable=False, default="unknown")
    changed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "target_type": self.target_type,
            "target_id": self.target_id,
            "field_name": self.field_name,
            "old_value": self.old_value,
            "new_value": self.new_value,
            "change_source": self.change_source,
            "changed_at": self.changed_at.isoformat() if self.changed_at else "",
        }


class EndpointProbe(Base):
    """ICMP/TCP probe result for an endpoint.

    Stores latency measurements and reachability status. Probes are
    operator-triggered (not automatic). Pruned at retention threshold.
    """
    __tablename__ = "endpoint_probes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    mac = Column(Text, nullable=False, index=True)
    ip = Column(Text, nullable=False)
    probe_type = Column(Text, nullable=False, default="icmp")  # icmp or tcp
    tcp_port = Column(Integer)  # only for TCP probes
    latency_ms = Column(Float)  # null if unreachable
    reachable = Column(Boolean, nullable=False, default=False)
    packet_loss = Column(Float)  # 0.0–1.0, ICMP only
    probed_at = Column(DateTime(timezone=True), default=_utcnow, nullable=False, index=True)

    def to_dict(self) -> dict:
        return {
            "id": self.id, "mac": self.mac, "ip": self.ip,
            "probe_type": self.probe_type, "tcp_port": self.tcp_port,
            "latency_ms": self.latency_ms, "reachable": self.reachable,
            "packet_loss": self.packet_loss,
            "probed_at": self.probed_at.isoformat() if self.probed_at else "",
        }


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

_db_ready: bool = False


# Sentinel table name used to detect "existing schema from the pre-Alembic era".
# Any core application table works — sweep_runs exists since Phase 2.5 and is
# present on every installation that has ever run db.init_db().
_SENTINEL_TABLE = "sweep_runs"


def _alembic_ini_path() -> str:
    """Return the path to alembic.ini.

    In the container: /app/alembic.ini.  In dev/test with the repo checked
    out: controller/alembic.ini.  ALEMBIC_CONFIG env var overrides for tests
    that point at a throwaway config.
    """
    override = os.environ.get("ALEMBIC_CONFIG")
    if override:
        return override
    # Running inside container: /app/alembic.ini sits alongside /app/app/db.py
    container_path = "/app/alembic.ini"
    if os.path.exists(container_path):
        return container_path
    # Dev / test path relative to this file
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, "..", "alembic.ini"))


async def _table_exists(conn, table_name: str) -> bool:
    """Return True if ``table_name`` exists in the connected database."""
    from sqlalchemy import text
    result = await conn.execute(
        text(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = :t"
        ),
        {"t": table_name},
    )
    return result.first() is not None


def _run_alembic_command(action: str) -> None:
    """Synchronous Alembic command runner — called via ``asyncio.to_thread``.

    action = "upgrade" → ``alembic upgrade head``
    action = "stamp"   → ``alembic stamp head`` (mark schema without running SQL)
    """
    from alembic import command
    from alembic.config import Config

    cfg = Config(_alembic_ini_path())
    if action == "upgrade":
        command.upgrade(cfg, "head")
    elif action == "stamp":
        command.stamp(cfg, "head")
    else:
        raise ValueError(f"Unknown alembic action: {action!r}")


async def _try_init_db() -> bool:
    """Single connection attempt — apply Alembic migrations. Returns True on success.

    Routes:
    - Fresh DB (no alembic_version, no app tables)  → upgrade head (creates tables)
    - Legacy DB (no alembic_version, app tables present) → stamp head (marks baseline)
    - Already-migrated DB (alembic_version present) → upgrade head (no-op or applies new)
    """
    async with engine.connect() as conn:
        has_alembic = await _table_exists(conn, "alembic_version")
        has_app_tables = await _table_exists(conn, _SENTINEL_TABLE)

    if has_alembic:
        action = "upgrade"
        path = "already_stamped"
    elif has_app_tables:
        action = "stamp"
        path = "legacy_schema_stamped"
    else:
        action = "upgrade"
        path = "fresh_install"

    log.info("db_init_alembic",
             "Applying Alembic migrations",
             context={"action": action, "path": path,
                      "has_alembic_version": has_alembic,
                      "has_app_tables": has_app_tables})

    await asyncio.to_thread(_run_alembic_command, action)

    # On legacy_schema_stamped we've only recorded the baseline — subsequent
    # migrations (if any) still need to run.  Harmless no-op on a fresh install
    # because we just upgraded to head above.
    if action == "stamp":
        await asyncio.to_thread(_run_alembic_command, "upgrade")

    return True


async def init_db() -> bool:
    """Wait up to DB_INIT_MAX_WAIT_SEC for Postgres, then create tables.

    On cold start the controller races Nautobot's bootstrap — the mnm_controller
    database doesn't exist until bootstrap creates it. This retry loop waits for
    the database to become available rather than failing immediately and falling
    back to JSON mode for the entire session.

    Returns True if connected and tables are ready, False if the timeout expired.
    """
    global _db_ready
    dsn_host = os.environ.get("MNM_DB_HOST", "postgres")
    elapsed = 0

    while elapsed < DB_INIT_MAX_WAIT_SEC:
        try:
            await _try_init_db()
            _db_ready = True
            log.info("db_init", "Controller database initialized",
                     context={"dsn_host": dsn_host, "waited_seconds": elapsed})
            return True
        except Exception as e:
            if elapsed == 0:
                log.info("db_init_waiting", "Database not ready — retrying",
                         context={"dsn_host": dsn_host, "error": str(e),
                                  "retry_interval_sec": DB_INIT_RETRY_INTERVAL_SEC,
                                  "max_wait_sec": DB_INIT_MAX_WAIT_SEC})
            else:
                log.info("db_init_retry", "Waiting for database",
                         context={"dsn_host": dsn_host, "elapsed_sec": elapsed})
            await asyncio.sleep(DB_INIT_RETRY_INTERVAL_SEC)
            elapsed += DB_INIT_RETRY_INTERVAL_SEC

    log.error("db_init_failed",
              "Controller database unavailable — falling back to JSON mode. "
              "Endpoint correlation, poll history, and sweep history are disabled. "
              "Run bootstrap and restart this container.",
              context={"dsn_host": dsn_host, "waited_sec": DB_INIT_MAX_WAIT_SEC})
    _db_ready = False
    return False


def is_ready() -> bool:
    return _db_ready


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with SessionLocal() as session:
        yield session
