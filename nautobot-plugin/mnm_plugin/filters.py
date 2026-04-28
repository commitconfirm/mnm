"""Django filter classes.

E1 ships ``EndpointFilterSet`` as a thin scaffold over the indexed
fields. E6 replaces this with the full per-column filters,
saved-filter presets, and expression-mode DSL specified in E0 §4.
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
        """``q=`` searches across MAC, IP, hostname, switch."""
        if not value:
            return queryset
        return queryset.filter(
            Q(mac_address__icontains=value)
            | Q(current_ip__icontains=value)
            | Q(hostname__icontains=value)
            | Q(current_switch__icontains=value)
        )
