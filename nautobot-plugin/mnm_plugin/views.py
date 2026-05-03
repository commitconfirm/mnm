"""Plugin views — list + detail per model.

E1 ships ``EndpointListView`` / ``EndpointView``. E2 adds:
  - ``ArpEntryListView`` / ``ArpEntryView``
  - ``MacEntryListView`` / ``MacEntryView``
  - ``LldpNeighborListView`` / ``LldpNeighborView``

E3 adds:
  - ``RouteListView`` / ``RouteView``
  - ``BgpNeighborListView`` / ``BgpNeighborView``
  - ``FingerprintListView`` / ``FingerprintView``

E4 enriches all seven detail views with three classes of panel:
  1. Cross-row history — prior observations of the same logical row
     (filter on the ``unique_together`` key, exclude current PK).
  2. Cross-model identity panels — MAC-keyed lookups across
     Endpoint / ArpEntry / MacEntry / Fingerprint, plus a Device-link
     resolution for LldpNeighbor.
  3. Recent Events read-through (Endpoint only) — controller's
     ``/api/endpoints/{mac}/history`` surfaced via the fail-soft
     ``utils.controller_client`` module.

Pattern enforced by E1's live-validation lessons (commit
``64684a3``) and E3's view-class-method lesson:
  - Use the concrete ``ObjectListView`` / ``ObjectView`` base
    classes from ``nautobot.apps.views``, NOT the mixins.
  - Class attributes (``queryset``, ``filterset``,
    ``filterset_form``, ``table``, ``template_name``) — no
    ``__init__`` overrides.
  - Detail-view extra context goes through
    ``get_extra_context(self, request, instance)`` — Django's
    ``get_context_data`` doesn't reach the Nautobot template
    rendering pipeline.
  - List-view custom context goes through ``extra_context()``
    (no args) — different signature on the list-view base.
"""

from nautobot.apps.views import ObjectListView, ObjectView

from mnm_plugin import filters, forms, models, tables
from mnm_plugin.utils import controller_client
from mnm_plugin.utils.interface import get_interface


# Cap for cross-row + cross-model panel result sets. Above this the
# template renders a "Show all" link to the relevant list view filtered
# by the same key. The 25-row cap matches the per-page default for
# Nautobot tables and keeps detail-page rendering snappy on
# heavily-populated MACs.
PANEL_LIMIT = 25


# ---------------------------------------------------------------------------
# Endpoint (E1, enriched in E4)
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
        # Existing E1 behavior: every (switch, port, vlan) row for
        # this MAC including inactive history, plus the union of all
        # IPs ever seen for the MAC.
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

        # E4: cross-row history — every other row for the same MAC,
        # excluding the current one. Same data as ``all_locations``
        # minus self; surfaces "where else has this MAC been?".
        history = (
            models.Endpoint.objects
            .filter(mac_address=instance.mac_address)
            .exclude(pk=instance.pk)
            .order_by("-last_seen")[:PANEL_LIMIT]
        )

        # E4: cross-model identity panels keyed on MAC.
        mac_table_observations = (
            models.MacEntry.objects
            .filter(mac=instance.mac_address)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )
        arp_observations = (
            models.ArpEntry.objects
            .filter(mac=instance.mac_address)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )
        fingerprints = (
            models.Fingerprint.objects
            .filter(target_mac=instance.mac_address)
            .order_by("-last_seen")[:PANEL_LIMIT]
        )

        # E4: cross-system Recent Events read-through. Always
        # fail-soft. Three rendering states keyed off return value.
        events = controller_client.get_endpoint_events_sync(
            instance.mac_address,
        )

        return {
            # E1
            "all_locations": all_locations,
            "all_ips": sorted(seen_ips),
            # E4 cross-row + cross-model
            "history": history,
            "mac_table_observations": mac_table_observations,
            "arp_observations": arp_observations,
            "fingerprints": fingerprints,
            # E4 controller read-through
            "events": events,
            "events_unavailable": events is None,
            "events_empty": events == [],
        }


# ---------------------------------------------------------------------------
# ArpEntry (E2, enriched in E4)
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
        # Existing E2 panel — kept so the existing template block
        # continues to render.
        same_ip_other_macs = (
            models.ArpEntry.objects.filter(ip=instance.ip)
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-row history — prior observations of the same
        # (node_name, ip, mac, vrf) quadruple. Mostly empty in steady
        # state because the upsert path replaces the row in place, but
        # populates when ``interface`` changes (MAC moves between
        # physical interfaces on the same node).
        history = (
            models.ArpEntry.objects
            .filter(
                node_name=instance.node_name,
                ip=instance.ip,
                mac=instance.mac,
                vrf=instance.vrf,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-model identity panels keyed on MAC.
        endpoint_records = (
            models.Endpoint.objects
            .filter(mac_address=instance.mac)
            .order_by("-active", "-last_seen")[:PANEL_LIMIT]
        )
        mac_table_observations = (
            models.MacEntry.objects
            .filter(mac=instance.mac)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        return {
            "same_ip_other_macs": list(same_ip_other_macs),
            "history": history,
            "endpoint_records": endpoint_records,
            "mac_table_observations": mac_table_observations,
        }


# ---------------------------------------------------------------------------
# MacEntry (E2, enriched in E4)
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
        # Existing E2 panel.
        same_mac_other_locations = (
            models.MacEntry.objects.filter(
                node_name=instance.node_name,
                mac=instance.mac,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-row history — prior observations of the same
        # (node_name, mac, interface, vlan) quadruple.
        history = (
            models.MacEntry.objects
            .filter(
                node_name=instance.node_name,
                mac=instance.mac,
                interface=instance.interface,
                vlan=instance.vlan,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-model identity panels keyed on MAC.
        endpoint_records = (
            models.Endpoint.objects
            .filter(mac_address=instance.mac)
            .order_by("-active", "-last_seen")[:PANEL_LIMIT]
        )
        arp_observations = (
            models.ArpEntry.objects
            .filter(mac=instance.mac)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        return {
            "same_mac_other_locations": list(same_mac_other_locations),
            "history": history,
            "endpoint_records": endpoint_records,
            "arp_observations": arp_observations,
        }


# ---------------------------------------------------------------------------
# LldpNeighbor (E2, enriched in E4)
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
        # Existing E2 panel.
        same_port_neighbors = (
            models.LldpNeighbor.objects.filter(
                node_name=instance.node_name,
                local_interface=instance.local_interface,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-row history — prior observations of the same
        # (node_name, local_interface, remote_system_name,
        # remote_port) quadruple. Surfaces neighbor turnover (a port
        # that historically saw one neighbor now seeing another).
        history = (
            models.LldpNeighbor.objects
            .filter(
                node_name=instance.node_name,
                local_interface=instance.local_interface,
                remote_system_name=instance.remote_system_name,
                remote_port=instance.remote_port,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: Device-link resolution for the remote side. If the
        # remote_system_name matches a Nautobot Device, render a
        # link. Goes through the cross-vendor naming helper for the
        # remote port lookup as well.
        remote_device = None
        remote_interface = None
        if instance.remote_system_name:
            try:
                from nautobot.dcim.models import Device
                remote_device = Device.objects.filter(
                    name=instance.remote_system_name,
                ).first()
            except Exception:  # noqa: BLE001
                remote_device = None
        if remote_device and instance.remote_port:
            remote_interface = get_interface(
                instance.remote_system_name, instance.remote_port,
            )

        return {
            "same_port_neighbors": list(same_port_neighbors),
            "history": history,
            "remote_device": remote_device,
            "remote_interface": remote_interface,
        }


# ---------------------------------------------------------------------------
# Route (E3, enriched in E4)
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
        # Existing E3 panel — same prefix on other nodes.
        same_prefix_other_nodes = (
            models.Route.objects.filter(prefix=instance.prefix)
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-row history — prior observations of the same
        # (node_name, prefix, next_hop, vrf) quadruple. Surfaces
        # next-hop changes over time on the same prefix (route
        # convergence / churn visibility).
        history = (
            models.Route.objects
            .filter(
                node_name=instance.node_name,
                prefix=instance.prefix,
                next_hop=instance.next_hop,
                vrf=instance.vrf,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        return {
            "same_prefix_other_nodes": list(same_prefix_other_nodes),
            "history": history,
        }


# ---------------------------------------------------------------------------
# BgpNeighbor (E3, enriched in E4)
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
        # Existing E3 panel — other neighbors on the same node.
        other_neighbors_on_node = (
            models.BgpNeighbor.objects.filter(node_name=instance.node_name)
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        # E4: cross-row history — prior observations of the same
        # (node_name, neighbor_ip, vrf, address_family) quadruple.
        # Surfaces state-flap history (Established → Idle →
        # Established).
        history = (
            models.BgpNeighbor.objects
            .filter(
                node_name=instance.node_name,
                neighbor_ip=instance.neighbor_ip,
                vrf=instance.vrf,
                address_family=instance.address_family,
            )
            .exclude(pk=instance.pk)
            .order_by("-collected_at")[:PANEL_LIMIT]
        )

        return {
            "other_neighbors_on_node": list(other_neighbors_on_node),
            "history": history,
        }


# ---------------------------------------------------------------------------
# Fingerprint (E3 — schema-only in v1.0, enriched in E4)
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
        # Existing E3 panels — cross-MAC + cross-signal sidebars.
        same_mac_other_signals = (
            models.Fingerprint.objects.filter(target_mac=instance.target_mac)
            .exclude(pk=instance.pk)
            .order_by("-last_seen")[:PANEL_LIMIT]
        )
        same_value_other_macs = (
            models.Fingerprint.objects.filter(
                signal_type=instance.signal_type,
                signal_value=instance.signal_value,
            )
            .exclude(pk=instance.pk)
            .order_by("-last_seen")[:PANEL_LIMIT]
        )

        # E4: cross-row history — prior observations of the same
        # (target_mac, signal_type, signal_value) triple. Mostly
        # empty in v1.0 (no production callers); v1.1 collectors
        # increment ``seen_count`` in place rather than insert a
        # new row, so this stays mostly empty in steady state too.
        history = (
            models.Fingerprint.objects
            .filter(
                target_mac=instance.target_mac,
                signal_type=instance.signal_type,
                signal_value=instance.signal_value,
            )
            .exclude(pk=instance.pk)
            .order_by("-last_seen")[:PANEL_LIMIT]
        )

        # E4: cross-model identity panel — Endpoint records for the
        # same MAC. Lets operators jump from a fingerprint signal to
        # the Endpoint that signal belongs to.
        endpoint_records = (
            models.Endpoint.objects
            .filter(mac_address=instance.target_mac)
            .order_by("-active", "-last_seen")[:PANEL_LIMIT]
        )

        return {
            "same_mac_other_signals": list(same_mac_other_signals),
            "same_value_other_macs": list(same_value_other_macs),
            "history": history,
            "endpoint_records": endpoint_records,
        }
