"""Endpoint list + detail views.

E1 ships:
  - ``EndpointListView`` — paginated, filterable list (filter
    framework is scaffold-level; E6 expands).
  - ``EndpointView`` — primary fields panel + "All locations
    seen" panel showing the full multi-record history for the
    same MAC.

Detail-view related panels deferred to later prompts:
  - "Recent events" (E4 — cross-system read-through to controller)
  - "Fingerprints" (E3 — Fingerprint model lands then)
"""

from django.shortcuts import get_object_or_404, render
from django.views.generic import DetailView, ListView

from nautobot.apps.views import (
    ObjectListViewMixin,
    ObjectDetailViewMixin,
)

from mnm_plugin import filters, forms, models, tables


class EndpointListView(ObjectListViewMixin, ListView):
    """List view: ``/plugins/mnm/endpoints/``.

    The Nautobot ``ObjectListViewMixin`` provides pagination,
    filterset wiring, table rendering, and CSV export.
    """

    queryset = models.Endpoint.objects.all()
    filterset_class = filters.EndpointFilterSet
    filterset_form_class = forms.EndpointFilterForm
    table_class = tables.EndpointTable
    template_name = "mnm_plugin/endpoint_list.html"
    action_buttons = ()


class EndpointView(ObjectDetailViewMixin, DetailView):
    """Detail view: ``/plugins/mnm/endpoints/<pk>/``."""

    queryset = models.Endpoint.objects.all()
    template_name = "mnm_plugin/endpoint_detail.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        endpoint = self.object
        # "All locations seen" panel: every Endpoint row sharing
        # this MAC, ordered by most-recent first. Includes
        # inactive history per E0 §3b.
        context["all_locations"] = (
            models.Endpoint.objects.filter(mac_address=endpoint.mac_address)
            .order_by("-active", "-last_seen")
        )
        # "All IPs ever seen" — derived by union over the same
        # MAC's rows.
        seen_ips: set = set()
        for row in context["all_locations"]:
            if row.current_ip:
                seen_ips.add(row.current_ip)
            for extra in row.additional_ips or []:
                if extra:
                    seen_ips.add(extra)
        context["all_ips"] = sorted(seen_ips)
        return context
