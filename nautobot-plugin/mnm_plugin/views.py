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


# ---------------------------------------------------------------------------
# Route (E3)
# ---------------------------------------------------------------------------

class RouteListView(ObjectListView):
    """List view: ``/plugins/mnm/routes/``."""

    queryset = models.Route.objects.all()
    filterset = filters.RouteFilterSet
    filterset_form = forms.RouteFilterForm
    table = tables.RouteTable
    template_name = "mnm_plugin/route_list.html"
    action_buttons = ()


class RouteView(ObjectView):
    """Detail view: ``/plugins/mnm/routes/<pk>/``."""

    queryset = models.Route.objects.all()
    template_name = "mnm_plugin/route_detail.html"

    def get_extra_context(self, request, instance):
        # Other Routes for the same prefix on different nodes —
        # surfaces ECMP fan-out and cross-node routing visibility
        # ("who else has this prefix?").
        same_prefix_other_nodes = (
            models.Route.objects.filter(prefix=instance.prefix)
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:25]
        )
        return {
            "same_prefix_other_nodes": list(same_prefix_other_nodes),
        }


# ---------------------------------------------------------------------------
# BgpNeighbor (E3)
# ---------------------------------------------------------------------------

class BgpNeighborListView(ObjectListView):
    """List view: ``/plugins/mnm/bgp-neighbors/``."""

    queryset = models.BgpNeighbor.objects.all()
    filterset = filters.BgpNeighborFilterSet
    filterset_form = forms.BgpNeighborFilterForm
    table = tables.BgpNeighborTable
    template_name = "mnm_plugin/bgpneighbor_list.html"
    action_buttons = ()


class BgpNeighborView(ObjectView):
    """Detail view: ``/plugins/mnm/bgp-neighbors/<pk>/``."""

    queryset = models.BgpNeighbor.objects.all()
    template_name = "mnm_plugin/bgpneighbor_detail.html"

    def get_extra_context(self, request, instance):
        # Other BGP neighbors on the same node — operational
        # context for "what's the BGP state of the device this
        # neighbor lives on?"
        other_neighbors_on_node = (
            models.BgpNeighbor.objects.filter(node_name=instance.node_name)
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:25]
        )
        return {
            "other_neighbors_on_node": list(other_neighbors_on_node),
        }


# ---------------------------------------------------------------------------
# Fingerprint (E3 — schema-only in v1.0)
# ---------------------------------------------------------------------------

class FingerprintListView(ObjectListView):
    """List view: ``/plugins/mnm/fingerprints/``.

    Empty state in v1.0 — the v1.1 fingerprinting workstream
    wires up the SSH host key / TLS cert / mDNS / NetBIOS /
    SNMPv3 EngineID / SSDP collectors. The template's
    ``is_v1_1_pending`` flag controls the empty-state callout.
    """

    queryset = models.Fingerprint.objects.all()
    filterset = filters.FingerprintFilterSet
    filterset_form = forms.FingerprintFilterForm
    table = tables.FingerprintTable
    template_name = "mnm_plugin/fingerprint_list.html"
    action_buttons = ()

    def extra_context(self):
        # Nautobot's ObjectListView passes ``extra_context()`` to
        # the template as additional template variables. We use
        # this to render the v1.1 callout when the table is empty
        # — i.e. when the unfiltered model has zero rows. The
        # callout disappears as soon as any signal lands, so v1.1
        # collectors don't need template changes.
        return {
            "is_v1_1_pending": not models.Fingerprint.objects.exists(),
        }


class FingerprintView(ObjectView):
    """Detail view: ``/plugins/mnm/fingerprints/<pk>/``."""

    queryset = models.Fingerprint.objects.all()
    template_name = "mnm_plugin/fingerprint_detail.html"

    def get_extra_context(self, request, instance):
        # Other Fingerprints with the same target_mac — surfaces
        # the cross-signal correlation v1.1 will use to assert
        # device identity.
        same_mac_other_signals = (
            models.Fingerprint.objects.filter(target_mac=instance.target_mac)
            .exclude(pk=instance.pk)
            .order_by("-last_seen")[:25]
        )
        # Other Fingerprints with the same signal_value across
        # MACs — "same device moved" detection (v1.1 cross-host
        # correlation).
        same_value_other_macs = (
            models.Fingerprint.objects.filter(
                signal_type=instance.signal_type,
                signal_value=instance.signal_value,
            )
            .exclude(pk=instance.pk)
            .order_by("-last_seen")[:25]
        )
        return {
            "same_mac_other_signals": list(same_mac_other_signals),
            "same_value_other_macs": list(same_value_other_macs),
        }
