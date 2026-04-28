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

from nautobot.apps.views import ObjectListView, ObjectView

from mnm_plugin import filters, forms, models, tables


class EndpointListView(ObjectListView):
    """List view: ``/plugins/mnm/endpoints/``."""

    queryset = models.Endpoint.objects.all()
    filterset = filters.EndpointFilterSet
    filterset_form = forms.EndpointFilterForm
    table = tables.EndpointTable
    template_name = "mnm_plugin/endpoint_list.html"
    action_buttons = ()


class EndpointView(ObjectView):
    """Detail view: ``/plugins/mnm/endpoints/<pk>/``."""

    queryset = models.Endpoint.objects.all()
    template_name = "mnm_plugin/endpoint_detail.html"

    def get_extra_context(self, request, instance):
        # "All locations seen" panel: every Endpoint row sharing
        # this MAC, ordered most-recent first. Includes inactive
        # history per E0 §3b.
        all_locations = (
            models.Endpoint.objects.filter(
                mac_address=instance.mac_address,
            ).order_by("-active", "-last_seen")
        )
        # "All IPs ever seen" — derived by union over the same
        # MAC's rows.
        seen_ips: set = set()
        for row in all_locations:
            if row.current_ip:
                seen_ips.add(row.current_ip)
            for extra in row.additional_ips or []:
                if extra:
                    seen_ips.add(extra)
        return {
            "all_locations": all_locations,
            "all_ips": sorted(seen_ips),
        }
