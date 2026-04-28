"""Plugin models.

E1 ships ``Endpoint``. E2 adds ``ArpEntry``, ``MacEntry``,
``LldpNeighbor`` (the link-layer triad). E3 adds ``Route``,
``BgpNeighbor``, ``Fingerprint``.

Schema mirrors the controller-side tables in
``controller/app/db.py``: ``Endpoint`` from lines 85-156,
``NodeArpEntry`` from 434-457, ``NodeMacEntry`` from 460-483,
``NodeLldpEntry`` from 486-526.

Per the "Schema convention" decision in CLAUDE.md, all text
columns use ``TextField`` — no ``CharField`` length bounds.

Endpoint uses ``BaseModel + ChangeLoggedModel`` because operators
edit endpoint metadata (classification overrides, hostname,
comments) and the change log is operator-relevant. ArpEntry,
MacEntry, LldpNeighbor are high-volume polling mirrors — they
inherit only ``BaseModel`` (UUID PK + timestamps) without the
change-log overhead, since rows are upserted every poll cycle
and a change log of every poll cycle is noise.
"""

from django.db import models
from django.utils import timezone

from nautobot.core.models import BaseModel
from nautobot.extras.models import ChangeLoggedModel


class Endpoint(BaseModel, ChangeLoggedModel):
    """MAC-on-port identity. Mirror of controller's ``endpoints``.

    Composite unique key: ``(mac_address, current_switch,
    current_port, current_vlan)``. A single MAC may have multiple
    rows when seen on more than one
    ``(switch, port, vlan)`` combination — the most recent
    location is the row with ``active=True``; prior locations
    persist as inactive history.
    """

    mac_address = models.TextField()
    current_switch = models.TextField()
    current_port = models.TextField()
    current_vlan = models.IntegerField()

    active = models.BooleanField(default=True)
    is_uplink = models.BooleanField(default=False)

    current_ip = models.TextField(null=True, blank=True)
    additional_ips = models.JSONField(default=list, blank=True)
    mac_vendor = models.TextField(null=True, blank=True)
    hostname = models.TextField(null=True, blank=True)
    classification = models.TextField(null=True, blank=True)
    classification_confidence = models.TextField(null=True, blank=True)
    classification_override = models.BooleanField(default=False)

    dhcp_server = models.TextField(null=True, blank=True)
    dhcp_lease_start = models.DateTimeField(null=True, blank=True)
    dhcp_lease_expiry = models.DateTimeField(null=True, blank=True)

    first_seen = models.DateTimeField(default=timezone.now)
    last_seen = models.DateTimeField(default=timezone.now)
    data_source = models.TextField(null=True, blank=True)

    class Meta:
        ordering = ("-last_seen", "mac_address")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "mac_address",
                    "current_switch",
                    "current_port",
                    "current_vlan",
                ],
                name="mnm_endpoint_unique_location",
            ),
        ]
        indexes = [
            models.Index(fields=["mac_address"]),
            models.Index(fields=["active"]),
            models.Index(fields=["current_ip"]),
            models.Index(fields=["last_seen"]),
        ]
        verbose_name = "Endpoint"
        verbose_name_plural = "Endpoints"

    def __str__(self) -> str:
        return f"{self.mac_address} on {self.current_switch}/{self.current_port}"

    def get_absolute_url(self):  # pragma: no cover - Nautobot routing
        from django.urls import reverse
        return reverse("plugins:mnm_plugin:endpoint", args=[self.pk])


class ArpEntry(BaseModel):
    """Per-node ARP table snapshot. Mirrors ``node_arp_entries``.

    Upserted every ARP poll cycle. Composite unique key on
    ``(node_name, ip, mac, vrf)``. ``interface`` carries the
    vendor-native ``ifName`` (Junos slot/port, Arista numeric,
    Fortinet alias, Cisco short, etc.) or the ``ifindex:N``
    sentinel when bridge-port → ifIndex resolution failed
    (per Block C P3/P4/P5 discipline).
    """

    node_name = models.TextField()
    ip = models.TextField()
    mac = models.TextField()
    interface = models.TextField(default="")
    vrf = models.TextField(default="default")
    collected_at = models.DateTimeField()

    class Meta:
        ordering = ("-collected_at", "node_name", "ip")
        constraints = [
            models.UniqueConstraint(
                fields=["node_name", "ip", "mac", "vrf"],
                name="mnm_arp_unique_node_ip_mac_vrf",
            ),
        ]
        indexes = [
            models.Index(fields=["node_name"]),
            models.Index(fields=["ip"]),
            models.Index(fields=["mac"]),
            models.Index(fields=["collected_at"]),
        ]
        verbose_name = "ARP entry"
        verbose_name_plural = "ARP entries"

    def __str__(self) -> str:
        return f"{self.node_name} {self.ip} → {self.mac}"

    def get_absolute_url(self):  # pragma: no cover
        from django.urls import reverse
        return reverse("plugins:mnm_plugin:arpentry", args=[self.pk])


class MacEntry(BaseModel):
    """Per-node MAC/FDB snapshot. Mirrors ``node_mac_entries``.

    Composite unique key on ``(node_name, mac, interface, vlan)``.
    ``interface`` may be the ``ifindex:N`` sentinel; ``entry_type``
    is ``"static"`` or ``"dynamic"`` (Block C P4 remap from
    Junos ``entry_status``).
    """

    node_name = models.TextField()
    mac = models.TextField()
    interface = models.TextField(default="")
    vlan = models.IntegerField(default=0)
    entry_type = models.TextField(default="dynamic")
    collected_at = models.DateTimeField()

    class Meta:
        ordering = ("-collected_at", "node_name", "mac")
        constraints = [
            models.UniqueConstraint(
                fields=["node_name", "mac", "interface", "vlan"],
                name="mnm_mac_unique_node_mac_iface_vlan",
            ),
        ]
        indexes = [
            models.Index(fields=["node_name"]),
            models.Index(fields=["mac"]),
            models.Index(fields=["vlan"]),
            models.Index(fields=["collected_at"]),
        ]
        verbose_name = "MAC entry"
        verbose_name_plural = "MAC entries"

    def __str__(self) -> str:
        return f"{self.node_name} {self.mac} vlan={self.vlan}"

    def get_absolute_url(self):  # pragma: no cover
        from django.urls import reverse
        return reverse("plugins:mnm_plugin:macentry", args=[self.pk])


class LldpNeighbor(BaseModel):
    """Per-node LLDP neighbor snapshot. Mirrors ``node_lldp_entries``.

    Composite unique key on
    ``(node_name, local_interface, remote_system_name, remote_port)``.
    The five expansion columns
    (``local_port_ifindex``, ``local_port_name``,
    ``remote_chassis_id_subtype``, ``remote_port_id_subtype``,
    ``remote_system_description``) come from Block C P2 schema
    expansion; populated by the SNMP LLDP collector.
    """

    node_name = models.TextField()
    local_interface = models.TextField()
    remote_system_name = models.TextField(default="")
    remote_port = models.TextField(default="")
    remote_chassis_id = models.TextField(null=True, blank=True)
    remote_management_ip = models.TextField(null=True, blank=True)
    local_port_ifindex = models.IntegerField(null=True, blank=True)
    local_port_name = models.TextField(null=True, blank=True)
    remote_chassis_id_subtype = models.TextField(null=True, blank=True)
    remote_port_id_subtype = models.TextField(null=True, blank=True)
    remote_system_description = models.TextField(null=True, blank=True)
    collected_at = models.DateTimeField()

    class Meta:
        ordering = ("-collected_at", "node_name", "local_interface")
        constraints = [
            models.UniqueConstraint(
                fields=[
                    "node_name",
                    "local_interface",
                    "remote_system_name",
                    "remote_port",
                ],
                name="mnm_lldp_unique_node_iface_remote",
            ),
        ]
        indexes = [
            models.Index(fields=["node_name"]),
            models.Index(fields=["local_interface"]),
            models.Index(fields=["remote_system_name"]),
            models.Index(fields=["collected_at"]),
        ]
        verbose_name = "LLDP neighbor"
        verbose_name_plural = "LLDP neighbors"

    def __str__(self) -> str:
        return (
            f"{self.node_name} {self.local_interface} ↔ "
            f"{self.remote_system_name or '?'} {self.remote_port or '?'}"
        )

    def get_absolute_url(self):  # pragma: no cover
        from django.urls import reverse
        return reverse("plugins:mnm_plugin:lldpneighbor", args=[self.pk])
