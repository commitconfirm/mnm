"""django-tables2 table definitions for plugin list views.

E1 ships ``EndpointTable``. E2 adds ``ArpEntryTable``,
``MacEntryTable``, ``LldpNeighborTable``. E3 adds ``RouteTable``,
``BgpNeighborTable``, ``FingerprintTable``.

Cross-link rendering uses the cross-vendor naming helper
(``utils/interface.py``) for any column that holds an interface
name. Sentinel rows (``ifindex:N``) render with a styled badge
and a tooltip — they never link to an Interface page.

E3 adds chip-rendering helpers for protocol (Route), state
(BgpNeighbor), and signal_type (Fingerprint), plus a humanized
duration helper for BGP uptime_seconds.
"""

from django.utils.html import format_html

import django_tables2 as tables

from nautobot.apps.tables import BaseTable, ToggleColumn

from mnm_plugin import models
from mnm_plugin.utils import interface as iface_utils


# ---------------------------------------------------------------------------
# Cell renderers shared across tables
# ---------------------------------------------------------------------------

def _render_device_link(value):
    """Link to Nautobot Device if one matches by name; else plain text."""
    if not value or value == "(none)":
        return value or "—"
    try:
        from nautobot.dcim.models import Device

        device = Device.objects.filter(name=value).first()
        if device:
            return format_html(
                '<a href="{}">{}</a>', device.get_absolute_url(), value,
            )
    except Exception:  # noqa: BLE001
        pass
    return value


def _render_interface_link(switch_name, iface_name):
    """Link to Nautobot Interface via cross-vendor naming helper.

    Sentinel rows (``ifindex:N``) get the warning-badge treatment.
    """
    if not iface_name or iface_name == "(none)":
        return iface_name or "—"
    if iface_utils.is_sentinel(iface_name):
        return format_html(
            '<span class="badge badge-warning" '
            'title="ifindex resolution failed; raw bridge port shown">'
            '{}</span>',
            iface_name,
        )
    iface = iface_utils.get_interface(switch_name, iface_name)
    if iface:
        return format_html(
            '<a href="{}">{}</a>', iface.get_absolute_url(), iface_name,
        )
    return iface_name


def _render_mac_link(value):
    """Link to Endpoint detail when a row exists for this MAC."""
    if not value:
        return "—"
    try:
        ep = models.Endpoint.objects.filter(
            mac_address__iexact=value, active=True,
        ).first()
        if ep:
            return format_html(
                '<a href="{}">{}</a>', ep.get_absolute_url(), value,
            )
    except Exception:  # noqa: BLE001
        pass
    return value


def _render_ip_link(value):
    """Link to Nautobot IPAddress when one matches by host."""
    if not value:
        return "—"
    try:
        from nautobot.ipam.models import IPAddress

        ip = IPAddress.objects.filter(host=value).first()
        if ip:
            return format_html(
                '<a href="{}">{}</a>', ip.get_absolute_url(), value,
            )
    except Exception:  # noqa: BLE001
        pass
    return value


# ---------------------------------------------------------------------------
# Endpoint (E1)
# ---------------------------------------------------------------------------

class EndpointTable(BaseTable):
    """Endpoint list view table."""

    pk = ToggleColumn()

    mac_address = tables.Column(linkify=True, verbose_name="MAC")
    current_ip = tables.Column(verbose_name="IP")
    current_switch = tables.Column(verbose_name="Switch")
    current_port = tables.Column(verbose_name="Port")
    current_vlan = tables.Column(verbose_name="VLAN")
    hostname = tables.Column(verbose_name="Hostname")
    classification = tables.Column(verbose_name="Classification")
    last_seen = tables.DateTimeColumn(verbose_name="Last Seen")

    class Meta(BaseTable.Meta):
        model = models.Endpoint
        fields = (
            "pk",
            "mac_address",
            "current_ip",
            "current_switch",
            "current_port",
            "current_vlan",
            "hostname",
            "classification",
            "last_seen",
        )
        default_columns = fields

    def render_current_switch(self, value, record):
        return _render_device_link(value)

    def render_current_port(self, value, record):
        return _render_interface_link(record.current_switch, value)

    def render_current_ip(self, value, record):
        return _render_ip_link(value)


# ---------------------------------------------------------------------------
# ArpEntry (E2)
# ---------------------------------------------------------------------------

class ArpEntryTable(BaseTable):
    """ARP entry list view table."""

    pk = ToggleColumn()

    node_name = tables.Column(verbose_name="Node")
    ip = tables.Column(verbose_name="IP")
    mac = tables.Column(verbose_name="MAC")
    interface = tables.Column(verbose_name="Interface")
    vrf = tables.Column(verbose_name="VRF")
    collected_at = tables.DateTimeColumn(verbose_name="Collected")

    class Meta(BaseTable.Meta):
        model = models.ArpEntry
        fields = (
            "pk",
            "node_name",
            "ip",
            "mac",
            "interface",
            "vrf",
            "collected_at",
        )
        default_columns = fields

    def render_node_name(self, value, record):
        return _render_device_link(value)

    def render_ip(self, value, record):
        return _render_ip_link(value)

    def render_mac(self, value, record):
        return _render_mac_link(value)

    def render_interface(self, value, record):
        return _render_interface_link(record.node_name, value)


# ---------------------------------------------------------------------------
# MacEntry (E2)
# ---------------------------------------------------------------------------

class MacEntryTable(BaseTable):
    """MAC entry list view table.

    ``entry_type`` renders as a colored chip — green for static,
    grey for dynamic. ``interface`` honors the sentinel
    ``ifindex:N`` semantics with the standard warning-badge.
    """

    pk = ToggleColumn()

    node_name = tables.Column(verbose_name="Node")
    mac = tables.Column(verbose_name="MAC")
    interface = tables.Column(verbose_name="Interface")
    vlan = tables.Column(verbose_name="VLAN")
    entry_type = tables.Column(verbose_name="Type")
    collected_at = tables.DateTimeColumn(verbose_name="Collected")

    class Meta(BaseTable.Meta):
        model = models.MacEntry
        fields = (
            "pk",
            "node_name",
            "mac",
            "interface",
            "vlan",
            "entry_type",
            "collected_at",
        )
        default_columns = fields

    def render_node_name(self, value, record):
        return _render_device_link(value)

    def render_mac(self, value, record):
        return _render_mac_link(value)

    def render_interface(self, value, record):
        return _render_interface_link(record.node_name, value)

    def render_entry_type(self, value, record):
        if (value or "").lower() == "static":
            return format_html(
                '<span class="label label-success">Static</span>',
            )
        return format_html(
            '<span class="label label-default">Dynamic</span>',
        )


# ---------------------------------------------------------------------------
# LldpNeighbor (E2)
# ---------------------------------------------------------------------------

class LldpNeighborTable(BaseTable):
    """LLDP neighbor list view table.

    The five expansion columns
    (``local_port_ifindex``, ``local_port_name``,
    ``remote_chassis_id_subtype``, ``remote_port_id_subtype``,
    ``remote_system_description``) are defined but hidden by
    default — operators toggle them via the column-chooser
    (per the Block C P2 schema-expansion lesson, these are
    diagnostic-only data and would clutter the default view).
    """

    pk = ToggleColumn()

    node_name = tables.Column(verbose_name="Node")
    local_interface = tables.Column(verbose_name="Local Interface")
    remote_system_name = tables.Column(verbose_name="Remote System")
    remote_port = tables.Column(verbose_name="Remote Port")
    remote_chassis_id = tables.Column(verbose_name="Remote Chassis ID")
    remote_management_ip = tables.Column(verbose_name="Remote Mgmt IP")
    local_port_ifindex = tables.Column(verbose_name="Local ifIndex")
    local_port_name = tables.Column(verbose_name="Local ifName")
    remote_chassis_id_subtype = tables.Column(verbose_name="Chassis ID Subtype")
    remote_port_id_subtype = tables.Column(verbose_name="Port ID Subtype")
    remote_system_description = tables.Column(verbose_name="Remote sysDescr")
    collected_at = tables.DateTimeColumn(verbose_name="Collected")

    class Meta(BaseTable.Meta):
        model = models.LldpNeighbor
        fields = (
            "pk",
            "node_name",
            "local_interface",
            "remote_system_name",
            "remote_port",
            "remote_chassis_id",
            "remote_management_ip",
            "local_port_ifindex",
            "local_port_name",
            "remote_chassis_id_subtype",
            "remote_port_id_subtype",
            "remote_system_description",
            "collected_at",
        )
        default_columns = (
            "pk",
            "node_name",
            "local_interface",
            "remote_system_name",
            "remote_port",
            "remote_chassis_id",
            "remote_management_ip",
            "collected_at",
        )

    def render_node_name(self, value, record):
        return _render_device_link(value)

    def render_local_interface(self, value, record):
        return _render_interface_link(record.node_name, value)

    def render_remote_management_ip(self, value, record):
        return _render_ip_link(value)


# ---------------------------------------------------------------------------
# Shared E3 cell renderers
# ---------------------------------------------------------------------------

# Route protocol → bootstrap label class. Keep the mapping
# explicit rather than algorithmic; new protocols slot in as
# operators encounter them. Default fallthrough is "default" (grey).
_PROTOCOL_LABEL_CLASS = {
    "bgp": "label-primary",
    "ospf": "label-info",
    "ospf3": "label-info",
    "isis": "label-info",
    "static": "label-warning",
    "connected": "label-success",
    "direct": "label-success",
    "local": "label-success",
    "rip": "label-info",
    "eigrp": "label-info",
}


def _render_protocol_chip(value):
    if not value:
        return "—"
    cls = _PROTOCOL_LABEL_CLASS.get(value.lower(), "label-default")
    return format_html('<span class="label {}">{}</span>', cls, value)


# BGP state → label class. Healthy states are green; failure /
# transitional states are red; everything else is grey.
_BGP_HEALTHY_STATES = {"established", "up"}
_BGP_FAILURE_STATES = {"idle", "active", "down", "connect", "opensent", "openconfirm"}


def _render_bgp_state_chip(value):
    if not value:
        return "—"
    lower = value.lower()
    if lower in _BGP_HEALTHY_STATES:
        cls = "label-success"
    elif lower in _BGP_FAILURE_STATES:
        cls = "label-danger"
    else:
        cls = "label-default"
    return format_html('<span class="label {}">{}</span>', cls, value)


def _render_bool_chip(value):
    if value is True:
        return format_html('<span class="label label-success">Yes</span>')
    if value is False:
        return format_html('<span class="label label-default">No</span>')
    return "—"


def _render_uptime(seconds):
    """Humanize uptime_seconds into ``Nd Nh`` / ``Nh Nm`` / ``Nm Ns``."""
    if seconds is None or seconds == "":
        return "—"
    try:
        s = int(seconds)
    except (TypeError, ValueError):
        return str(seconds)
    if s < 0:
        return "—"
    days, rem = divmod(s, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    if days >= 1:
        return f"{days}d {hours}h"
    if hours >= 1:
        return f"{hours}h {minutes}m"
    if minutes >= 1:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


# Fingerprint signal_type → label class. Each signal source gets
# its own color so operators can spot patterns at a glance.
_SIGNAL_TYPE_LABEL_CLASS = {
    "ssh_hostkey": "label-primary",
    "tls_cert": "label-info",
    "snmpv3_engineid": "label-warning",
    "mdns": "label-success",
    "netbios": "label-default",
    "ssdp": "label-default",
}


def _render_signal_type_chip(value):
    if not value:
        return "—"
    cls = _SIGNAL_TYPE_LABEL_CLASS.get(value.lower(), "label-default")
    return format_html('<span class="label {}">{}</span>', cls, value)


def _render_truncated(value, limit=80):
    """Render with full-value tooltip when over ``limit`` chars."""
    if not value:
        return "—"
    if len(value) <= limit:
        return value
    return format_html(
        '<span title="{}">{}…</span>',
        value,
        value[:limit],
    )


# ---------------------------------------------------------------------------
# Route (E3)
# ---------------------------------------------------------------------------

class RouteTable(BaseTable):
    """Routing table snapshot list view.

    ``metric`` / ``preference`` / ``outgoing_interface`` are
    defined but hidden by default; operators toggle via the
    column-chooser. Default columns optimize for the common
    "what's in this VRF's routing table?" question.
    """

    pk = ToggleColumn()

    node_name = tables.Column(verbose_name="Node")
    prefix = tables.Column(verbose_name="Prefix")
    next_hop = tables.Column(verbose_name="Next Hop")
    protocol = tables.Column(verbose_name="Protocol")
    vrf = tables.Column(verbose_name="VRF")
    metric = tables.Column(verbose_name="Metric")
    preference = tables.Column(verbose_name="Preference")
    outgoing_interface = tables.Column(verbose_name="Out Interface")
    active = tables.Column(verbose_name="Active")
    collected_at = tables.DateTimeColumn(verbose_name="Collected")

    class Meta(BaseTable.Meta):
        model = models.Route
        fields = (
            "pk",
            "node_name",
            "prefix",
            "next_hop",
            "protocol",
            "vrf",
            "metric",
            "preference",
            "outgoing_interface",
            "active",
            "collected_at",
        )
        default_columns = (
            "pk",
            "node_name",
            "prefix",
            "next_hop",
            "protocol",
            "vrf",
            "active",
            "collected_at",
        )

    def render_node_name(self, value, record):
        return _render_device_link(value)

    def render_next_hop(self, value, record):
        return _render_ip_link(value)

    def render_protocol(self, value, record):
        return _render_protocol_chip(value)

    def render_outgoing_interface(self, value, record):
        # Cross-vendor naming helper for display rendering only.
        # No sentinel ifindex:N here — Routes don't go through
        # bridge-port resolution. dcim.Interface lookup is E5's
        # stress-test; for E3 just render via the helper.
        if not value:
            return "—"
        canonical, original = iface_utils.normalize(value)
        # Try to link to dcim.Interface; fall back to plain
        # canonical text if no match.
        iface = iface_utils.get_interface(record.node_name, value)
        if iface:
            return format_html(
                '<a href="{}">{}</a>', iface.get_absolute_url(), original,
            )
        # Show original (preserves vendor-native form for
        # operator inspection); canonical lookup happened
        # internally and is identical when no transform applies.
        return original

    def render_active(self, value, record):
        return _render_bool_chip(value)


# ---------------------------------------------------------------------------
# BgpNeighbor (E3)
# ---------------------------------------------------------------------------

class BgpNeighborTable(BaseTable):
    """BGP neighbor list view with state chip + humanized
    uptime."""

    pk = ToggleColumn()

    node_name = tables.Column(verbose_name="Node")
    neighbor_ip = tables.Column(verbose_name="Neighbor IP")
    remote_asn = tables.Column(verbose_name="Remote ASN")
    local_asn = tables.Column(verbose_name="Local ASN")
    state = tables.Column(verbose_name="State")
    vrf = tables.Column(verbose_name="VRF")
    address_family = tables.Column(verbose_name="AFI")
    prefixes_received = tables.Column(verbose_name="Prefixes In")
    prefixes_sent = tables.Column(verbose_name="Prefixes Out")
    uptime_seconds = tables.Column(verbose_name="Uptime")
    collected_at = tables.DateTimeColumn(verbose_name="Collected")

    class Meta(BaseTable.Meta):
        model = models.BgpNeighbor
        fields = (
            "pk",
            "node_name",
            "neighbor_ip",
            "remote_asn",
            "local_asn",
            "state",
            "vrf",
            "address_family",
            "prefixes_received",
            "prefixes_sent",
            "uptime_seconds",
            "collected_at",
        )
        default_columns = (
            "pk",
            "node_name",
            "neighbor_ip",
            "remote_asn",
            "state",
            "vrf",
            "address_family",
            "prefixes_received",
            "uptime_seconds",
            "collected_at",
        )

    def render_node_name(self, value, record):
        return _render_device_link(value)

    def render_neighbor_ip(self, value, record):
        return _render_ip_link(value)

    def render_state(self, value, record):
        return _render_bgp_state_chip(value)

    def render_uptime_seconds(self, value, record):
        return _render_uptime(value)


# ---------------------------------------------------------------------------
# Fingerprint (E3 — schema-only in v1.0)
# ---------------------------------------------------------------------------

class FingerprintTable(BaseTable):
    """Fingerprint signal list view.

    v1.0 ships schema-only — no signal collectors are wired yet.
    The list view renders a v1.1-pending callout when empty
    (handled in the template / view, not the table). This table
    works correctly when populated; the v1.1 fingerprinting
    workstream just adds the upstream collectors.
    """

    pk = ToggleColumn()

    target_mac = tables.Column(verbose_name="Target MAC")
    signal_type = tables.Column(verbose_name="Signal Type")
    signal_value = tables.Column(verbose_name="Signal Value")
    seen_count = tables.Column(verbose_name="Seen #")
    first_seen = tables.DateTimeColumn(verbose_name="First Seen")
    last_seen = tables.DateTimeColumn(verbose_name="Last Seen")

    class Meta(BaseTable.Meta):
        model = models.Fingerprint
        fields = (
            "pk",
            "target_mac",
            "signal_type",
            "signal_value",
            "seen_count",
            "first_seen",
            "last_seen",
        )
        default_columns = fields

    def render_target_mac(self, value, record):
        return _render_mac_link(value)

    def render_signal_type(self, value, record):
        return _render_signal_type_chip(value)

    def render_signal_value(self, value, record):
        return _render_truncated(value, limit=80)
