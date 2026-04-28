"""Plugin views — list + detail per model.

E1 ships ``EndpointListView`` / ``EndpointView``. E2 adds:
  - ``ArpEntryListView`` / ``ArpEntryView``
  - ``MacEntryListView`` / ``MacEntryView``
  - ``LldpNeighborListView`` / ``LldpNeighborView``

Pattern enforced by E1's live-validation lessons (commit
``64684a3``):
  - Use the concrete ``ObjectListView`` / ``ObjectView`` base
    classes from ``nautobot.apps.views``, NOT the mixins.
  - Class attributes (``queryset``, ``filterset``,
    ``filterset_form``, ``table``, ``template_name``) — no
    ``__init__`` overrides.
  - Detail view extra context goes through
    ``get_extra_context(self, request, instance)`` — Django's
    ``get_context_data`` doesn't reach the Nautobot template
    rendering pipeline.
"""

from nautobot.apps.views import ObjectListView, ObjectView

from mnm_plugin import filters, forms, models, tables


# ---------------------------------------------------------------------------
# Endpoint (E1)
# ---------------------------------------------------------------------------

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
        all_locations = (
            models.Endpoint.objects.filter(
                mac_address=instance.mac_address,
            ).order_by("-active", "-last_seen")
        )
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


# ---------------------------------------------------------------------------
# ArpEntry (E2)
# ---------------------------------------------------------------------------

class ArpEntryListView(ObjectListView):
    """List view: ``/plugins/mnm/arp-entries/``."""

    queryset = models.ArpEntry.objects.all()
    filterset = filters.ArpEntryFilterSet
    filterset_form = forms.ArpEntryFilterForm
    table = tables.ArpEntryTable
    template_name = "mnm_plugin/arpentry_list.html"
    action_buttons = ()


class ArpEntryView(ObjectView):
    """Detail view: ``/plugins/mnm/arp-entries/<pk>/``."""

    queryset = models.ArpEntry.objects.all()
    template_name = "mnm_plugin/arpentry_detail.html"

    def get_extra_context(self, request, instance):
        # Recent prior collections of the same (node, ip, mac, vrf)
        # tuple — the controller-side row is upserted in place,
        # so prior collections aren't visible at the plugin
        # level. Surface other ARP entries for the same
        # (node_name, ip) so operators see what else is at
        # this address.
        same_ip_other_macs = (
            models.ArpEntry.objects.filter(ip=instance.ip)
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:25]
        )
        return {
            "same_ip_other_macs": list(same_ip_other_macs),
        }


# ---------------------------------------------------------------------------
# MacEntry (E2)
# ---------------------------------------------------------------------------

class MacEntryListView(ObjectListView):
    """List view: ``/plugins/mnm/mac-entries/``."""

    queryset = models.MacEntry.objects.all()
    filterset = filters.MacEntryFilterSet
    filterset_form = forms.MacEntryFilterForm
    table = tables.MacEntryTable
    template_name = "mnm_plugin/macentry_list.html"
    action_buttons = ()


class MacEntryView(ObjectView):
    """Detail view: ``/plugins/mnm/mac-entries/<pk>/``."""

    queryset = models.MacEntry.objects.all()
    template_name = "mnm_plugin/macentry_detail.html"

    def get_extra_context(self, request, instance):
        # Other (interface, vlan) appearances of the same MAC on
        # the same node — captures situations where a MAC roams
        # within one switch.
        same_mac_other_locations = (
            models.MacEntry.objects.filter(
                node_name=instance.node_name,
                mac=instance.mac,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:25]
        )
        return {
            "same_mac_other_locations": list(same_mac_other_locations),
        }


# ---------------------------------------------------------------------------
# LldpNeighbor (E2)
# ---------------------------------------------------------------------------

class LldpNeighborListView(ObjectListView):
    """List view: ``/plugins/mnm/lldp-neighbors/``."""

    queryset = models.LldpNeighbor.objects.all()
    filterset = filters.LldpNeighborFilterSet
    filterset_form = forms.LldpNeighborFilterForm
    table = tables.LldpNeighborTable
    template_name = "mnm_plugin/lldpneighbor_list.html"
    action_buttons = ()


class LldpNeighborView(ObjectView):
    """Detail view: ``/plugins/mnm/lldp-neighbors/<pk>/``."""

    queryset = models.LldpNeighbor.objects.all()
    template_name = "mnm_plugin/lldpneighbor_detail.html"

    def get_extra_context(self, request, instance):
        # Other neighbors visible to the same local_interface —
        # rare in well-formed networks but useful when a port
        # sees multiple LLDP-speakers (e.g., daisy-chained
        # phones).
        same_port_neighbors = (
            models.LldpNeighbor.objects.filter(
                node_name=instance.node_name,
                local_interface=instance.local_interface,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:25]
        )
        return {
            "same_port_neighbors": list(same_port_neighbors),
        }
