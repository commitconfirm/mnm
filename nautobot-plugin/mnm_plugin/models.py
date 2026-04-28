"""Plugin models.

E1 ships ``Endpoint`` only. E2 adds ``ArpEntry``, ``MacEntry``,
``LldpNeighbor``. E3 adds ``Route``, ``BgpNeighbor``, ``Fingerprint``.

Schema mirrors the controller-side table at
``controller/app/db.py::Endpoint`` (lines 85-156). Per the
"Schema convention" decision in CLAUDE.md, all text columns use
``TextField`` — no ``CharField`` length bounds.

The composite unique constraint on
``(mac_address, current_switch, current_port, current_vlan)``
preserves Netdisco-style multi-record history per MAC. Sentinel
values ``"(none)"`` (for switch / port) and ``0`` (for vlan) are
allowed and meaningful — they identify sweep-only endpoints.

Per E0 §2c the model uses Nautobot's standard ``BaseModel``
mixin so it inherits UUID primary key, ``created`` /
``last_updated`` timestamps, and Nautobot's change-log /
custom-field machinery.
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
