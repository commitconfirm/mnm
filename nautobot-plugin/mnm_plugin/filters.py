"""Django filter classes (E6).

E1 shipped scaffold-level per-column filters. E2/E3 extended for
the link-layer + L3/identity triads. E6 wires up the full filter
framework:

  - Per-column filters preserved (Layer 1 of the UI)
  - ``preset_*`` BooleanFilter choices for saved-filter chips
    (Layer 2 of the UI). Five presets on Endpoint
    (duplicate_ips, multi_homed, stale, unmapped, no_dns); a
    single ``preset_stale`` on the five polling-mirror models
    (ArpEntry, MacEntry, LldpNeighbor, Route, BgpNeighbor).
    Fingerprint gets none — schema-only in v1.0 with no presets
    that fit the empty-table state.
  - ``q`` repurposed as the expression-mode DSL field (Layer 3
    of the UI). Replaces E1's free-text multi-field icontains
    search — per-column filters subsume that case. Parse errors
    surface via the view's ``extra_context()`` (parsed twice on
    purpose: filterset applies, view surfaces — clean separation,
    parser cost is microseconds).
  - Per E0 §7 Q8 (locked decision): presets are code-defined
    globals. Per-user saved filters is v1.1.
"""

from datetime import timedelta

import django_filters
from django.db.models import Count, Q
from django.utils import timezone

from nautobot.apps.filters import NautobotFilterSet

from mnm_plugin import models
from mnm_plugin.filter_dsl import DslError, parse_dsl


# Default age cut-off for the "stale entries" preset chip.
# Operators wanting a different threshold use expression mode:
# ``collected_at < "30 days ago"``.
STALE_DEFAULT_DAYS = 7


def _apply_dsl(queryset, value: str, allowlist):
    """Parse the DSL value and apply.

    On parse failure, return the queryset unchanged — the view's
    ``extra_context()`` re-parses the same expression to surface
    the error message in the UI banner.
    """
    if not value:
        return queryset
    result = parse_dsl(value, allowlist)
    if isinstance(result, DslError):
        return queryset
    return queryset.filter(result)


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


class EndpointFilterSet(NautobotFilterSet):
    """Endpoint list filter set."""

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
    mac_address = django_filters.CharFilter(lookup_expr="icontains")
    current_ip = django_filters.CharFilter(lookup_expr="icontains")
    current_switch = django_filters.CharFilter(lookup_expr="icontains")
    current_port = django_filters.CharFilter(lookup_expr="icontains")
    current_vlan = django_filters.NumberFilter()
    active = django_filters.BooleanFilter()
    classification = django_filters.CharFilter(lookup_expr="iexact")
    hostname = django_filters.CharFilter(lookup_expr="icontains")
    last_seen = django_filters.IsoDateTimeFilter()

    preset_duplicate_ips = django_filters.BooleanFilter(
        method="filter_duplicate_ips",
    )
    preset_multi_homed = django_filters.BooleanFilter(
        method="filter_multi_homed",
    )
    preset_stale = django_filters.BooleanFilter(method="filter_stale")
    preset_unmapped = django_filters.BooleanFilter(method="filter_unmapped")
    preset_no_dns = django_filters.BooleanFilter(method="filter_no_dns")

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
            "hostname",
            "last_seen",
        ]

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)

    def filter_duplicate_ips(self, queryset, name, value):
        if not value:
            return queryset
        duplicate_ips = (
            models.Endpoint.objects
            .filter(current_ip__isnull=False)
            .exclude(current_ip="")
            .values("current_ip")
            .annotate(group_count=Count("current_ip"))
            .filter(group_count__gt=1)
            .values_list("current_ip", flat=True)
        )
        return queryset.filter(current_ip__in=list(duplicate_ips))

    def filter_multi_homed(self, queryset, name, value):
        if not value:
            return queryset
        multi_homed_macs = (
            models.Endpoint.objects
            .values("mac_address")
            .annotate(
                switch_count=Count("current_switch", distinct=True),
            )
            .filter(switch_count__gt=1)
            .values_list("mac_address", flat=True)
        )
        return queryset.filter(mac_address__in=list(multi_homed_macs))

    def filter_stale(self, queryset, name, value):
        if not value:
            return queryset
        cutoff = timezone.now() - timedelta(days=STALE_DEFAULT_DAYS)
        return queryset.filter(last_seen__lt=cutoff)

    def filter_unmapped(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(
            Q(current_switch="(none)") | Q(current_port="(none)"),
        )

    def filter_no_dns(self, queryset, name, value):
        if not value:
            return queryset
        return queryset.filter(Q(hostname__isnull=True) | Q(hostname=""))


# ---------------------------------------------------------------------------
# ArpEntry
# ---------------------------------------------------------------------------


class ArpEntryFilterSet(NautobotFilterSet):
    """ArpEntry list filter set."""

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    ip = django_filters.CharFilter(lookup_expr="icontains")
    mac = django_filters.CharFilter(lookup_expr="icontains")
    interface = django_filters.CharFilter(lookup_expr="icontains")
    vrf = django_filters.CharFilter(lookup_expr="iexact")
    collected_at = django_filters.IsoDateTimeFilter()

    preset_stale = django_filters.BooleanFilter(method="filter_stale")

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

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)

    def filter_stale(self, queryset, name, value):
        if not value:
            return queryset
        cutoff = timezone.now() - timedelta(days=STALE_DEFAULT_DAYS)
        return queryset.filter(collected_at__lt=cutoff)


# ---------------------------------------------------------------------------
# MacEntry
# ---------------------------------------------------------------------------


class MacEntryFilterSet(NautobotFilterSet):
    """MacEntry list filter set."""

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    mac = django_filters.CharFilter(lookup_expr="icontains")
    interface = django_filters.CharFilter(lookup_expr="icontains")
    vlan = django_filters.NumberFilter()
    entry_type = django_filters.ChoiceFilter(
        choices=[("static", "static"), ("dynamic", "dynamic")],
    )
    collected_at = django_filters.IsoDateTimeFilter()

    preset_stale = django_filters.BooleanFilter(method="filter_stale")

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

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)

    def filter_stale(self, queryset, name, value):
        if not value:
            return queryset
        cutoff = timezone.now() - timedelta(days=STALE_DEFAULT_DAYS)
        return queryset.filter(collected_at__lt=cutoff)


# ---------------------------------------------------------------------------
# LldpNeighbor
# ---------------------------------------------------------------------------


class LldpNeighborFilterSet(NautobotFilterSet):
    """LldpNeighbor list filter set."""

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    local_interface = django_filters.CharFilter(lookup_expr="icontains")
    remote_system_name = django_filters.CharFilter(lookup_expr="icontains")
    remote_port = django_filters.CharFilter(lookup_expr="icontains")
    remote_chassis_id = django_filters.CharFilter(lookup_expr="icontains")
    remote_management_ip = django_filters.CharFilter(lookup_expr="icontains")
    collected_at = django_filters.IsoDateTimeFilter()

    preset_stale = django_filters.BooleanFilter(method="filter_stale")

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

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)

    def filter_stale(self, queryset, name, value):
        if not value:
            return queryset
        cutoff = timezone.now() - timedelta(days=STALE_DEFAULT_DAYS)
        return queryset.filter(collected_at__lt=cutoff)


# ---------------------------------------------------------------------------
# Route
# ---------------------------------------------------------------------------


class RouteFilterSet(NautobotFilterSet):
    """Route list filter set."""

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    prefix = django_filters.CharFilter(lookup_expr="icontains")
    next_hop = django_filters.CharFilter(lookup_expr="icontains")
    protocol = django_filters.CharFilter(lookup_expr="iexact")
    vrf = django_filters.CharFilter(lookup_expr="iexact")
    active = django_filters.BooleanFilter()
    collected_at = django_filters.IsoDateTimeFilter()

    preset_stale = django_filters.BooleanFilter(method="filter_stale")

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

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)

    def filter_stale(self, queryset, name, value):
        if not value:
            return queryset
        cutoff = timezone.now() - timedelta(days=STALE_DEFAULT_DAYS)
        return queryset.filter(collected_at__lt=cutoff)


# ---------------------------------------------------------------------------
# BgpNeighbor
# ---------------------------------------------------------------------------


class BgpNeighborFilterSet(NautobotFilterSet):
    """BgpNeighbor list filter set."""

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
    node_name = django_filters.CharFilter(lookup_expr="icontains")
    neighbor_ip = django_filters.CharFilter(lookup_expr="icontains")
    remote_asn = django_filters.NumberFilter()
    state = django_filters.CharFilter(lookup_expr="iexact")
    vrf = django_filters.CharFilter(lookup_expr="iexact")
    address_family = django_filters.CharFilter(lookup_expr="iexact")
    collected_at = django_filters.IsoDateTimeFilter()

    preset_stale = django_filters.BooleanFilter(method="filter_stale")

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

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)

    def filter_stale(self, queryset, name, value):
        if not value:
            return queryset
        cutoff = timezone.now() - timedelta(days=STALE_DEFAULT_DAYS)
        return queryset.filter(collected_at__lt=cutoff)


# ---------------------------------------------------------------------------
# Fingerprint (no presets in v1.0 — schema-only)
# ---------------------------------------------------------------------------


class FingerprintFilterSet(NautobotFilterSet):
    """Fingerprint list filter set.

    No presets in v1.0 — the table is empty (signal collection is
    a v1.1 workstream). Per-column filters and DSL still work.
    """

    q = django_filters.CharFilter(method="apply_dsl", label="Expression")
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

    def apply_dsl(self, queryset, name, value):
        return _apply_dsl(queryset, value, self.Meta.fields)
