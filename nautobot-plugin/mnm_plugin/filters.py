"""Django filter classes.

E1 ships ``EndpointFilterSet``. E2 adds ``ArpEntryFilterSet``,
``MacEntryFilterSet``, ``LldpNeighborFilterSet``.

These are scaffold-level (per-column filters on indexed fields).
E6 (filter framework) replaces with the full saved-presets +
expression-mode DSL specified in E0 §4.
"""

import django_filters
from django.db.models import Q

from nautobot.apps.filters import NautobotFilterSet

from mnm_plugin import models


class EndpointFilterSet(NautobotFilterSet):
    """Endpoint list filter set (E1 scaffold)."""

    q = django_filters.CharFilter(method="search", label="Search")
    mac_address = django_filters.CharFilter(lookup_expr="icontains")
    current_ip = django_filters.CharFilter(lookup_expr="icontains")
    current_switch = django_filters.CharFilter(lookup_expr="icontains")
    current_port = django_filters.CharFilter(lookup_expr="icontains")
    current_vlan = django_filters.NumberFilter()
    active = django_filters.BooleanFilter()
    classification = django_filters.CharFilter(lookup_expr="iexact")
    last_seen = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.Endpoint
        fields = [
            "mac_address",
            "current_ip",
            "current_switch",
            "current_port",
            "current_vlan",
            "active",
            "classification",
            "last_seen",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(mac_address__icontains=value)
            | Q(current_ip__icontains=value)
            | Q(hostname__icontains=value)
            | Q(current_switch__icontains=value)
        )


class ArpEntryFilterSet(NautobotFilterSet):
    """ArpEntry list filter set (E2 scaffold)."""

    q = django_filters.CharFilter(method="search", label="Search")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    ip = django_filters.CharFilter(lookup_expr="icontains")
    mac = django_filters.CharFilter(lookup_expr="icontains")
    interface = django_filters.CharFilter(lookup_expr="icontains")
    vrf = django_filters.CharFilter(lookup_expr="iexact")
    collected_at = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.ArpEntry
        fields = [
            "node_name",
            "ip",
            "mac",
            "interface",
            "vrf",
            "collected_at",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(node_name__icontains=value)
            | Q(ip__icontains=value)
            | Q(mac__icontains=value)
            | Q(interface__icontains=value)
        )


class MacEntryFilterSet(NautobotFilterSet):
    """MacEntry list filter set (E2 scaffold)."""

    q = django_filters.CharFilter(method="search", label="Search")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    mac = django_filters.CharFilter(lookup_expr="icontains")
    interface = django_filters.CharFilter(lookup_expr="icontains")
    vlan = django_filters.NumberFilter()
    entry_type = django_filters.ChoiceFilter(
        choices=[("static", "static"), ("dynamic", "dynamic")],
    )
    collected_at = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.MacEntry
        fields = [
            "node_name",
            "mac",
            "interface",
            "vlan",
            "entry_type",
            "collected_at",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(node_name__icontains=value)
            | Q(mac__icontains=value)
            | Q(interface__icontains=value)
        )


class LldpNeighborFilterSet(NautobotFilterSet):
    """LldpNeighbor list filter set (E2 scaffold)."""

    q = django_filters.CharFilter(method="search", label="Search")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    local_interface = django_filters.CharFilter(lookup_expr="icontains")
    remote_system_name = django_filters.CharFilter(lookup_expr="icontains")
    remote_port = django_filters.CharFilter(lookup_expr="icontains")
    remote_chassis_id = django_filters.CharFilter(lookup_expr="icontains")
    remote_management_ip = django_filters.CharFilter(lookup_expr="icontains")
    collected_at = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.LldpNeighbor
        fields = [
            "node_name",
            "local_interface",
            "remote_system_name",
            "remote_port",
            "remote_chassis_id",
            "remote_management_ip",
            "collected_at",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(node_name__icontains=value)
            | Q(local_interface__icontains=value)
            | Q(remote_system_name__icontains=value)
            | Q(remote_port__icontains=value)
            | Q(remote_chassis_id__icontains=value)
        )


class RouteFilterSet(NautobotFilterSet):
    """Route list filter set (E3 scaffold)."""

    q = django_filters.CharFilter(method="search", label="Search")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    prefix = django_filters.CharFilter(lookup_expr="icontains")
    next_hop = django_filters.CharFilter(lookup_expr="icontains")
    protocol = django_filters.CharFilter(lookup_expr="iexact")
    vrf = django_filters.CharFilter(lookup_expr="iexact")
    active = django_filters.BooleanFilter()
    collected_at = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.Route
        fields = [
            "node_name",
            "prefix",
            "next_hop",
            "protocol",
            "vrf",
            "active",
            "collected_at",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(node_name__icontains=value)
            | Q(prefix__icontains=value)
            | Q(next_hop__icontains=value)
            | Q(outgoing_interface__icontains=value)
        )


class BgpNeighborFilterSet(NautobotFilterSet):
    """BgpNeighbor list filter set (E3 scaffold)."""

    q = django_filters.CharFilter(method="search", label="Search")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    neighbor_ip = django_filters.CharFilter(lookup_expr="icontains")
    remote_asn = django_filters.NumberFilter()
    state = django_filters.CharFilter(lookup_expr="iexact")
    vrf = django_filters.CharFilter(lookup_expr="iexact")
    address_family = django_filters.CharFilter(lookup_expr="iexact")
    collected_at = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.BgpNeighbor
        fields = [
            "node_name",
            "neighbor_ip",
            "remote_asn",
            "state",
            "vrf",
            "address_family",
            "collected_at",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(node_name__icontains=value)
            | Q(neighbor_ip__icontains=value)
            | Q(state__icontains=value)
        )


class FingerprintFilterSet(NautobotFilterSet):
    """Fingerprint list filter set (E3 scaffold).

    v1.0 ships schema-only; the filterset works once the v1.1
    signal collectors populate rows.
    """

    q = django_filters.CharFilter(method="search", label="Search")
    target_mac = django_filters.CharFilter(lookup_expr="icontains")
    signal_type = django_filters.CharFilter(lookup_expr="iexact")
    signal_value = django_filters.CharFilter(lookup_expr="icontains")
    last_seen = django_filters.IsoDateTimeFilter()

    class Meta:
        model = models.Fingerprint
        fields = [
            "target_mac",
            "signal_type",
            "signal_value",
            "last_seen",
        ]

    def search(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(target_mac__icontains=value)
            | Q(signal_type__icontains=value)
            | Q(signal_value__icontains=value)
        )
