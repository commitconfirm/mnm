"""django-tables2 table definitions for plugin list views.

E1 ships ``EndpointTable``. E2 adds ``ArpEntryTable``,
``MacEntryTable``, ``LldpNeighborTable``.

Cross-link rendering uses the cross-vendor naming helper
(``utils/interface.py``) for any column that holds an interface
name. Sentinel rows (``ifindex:N``) render with a styled badge
and a tooltip — they never link to an Interface page.
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
