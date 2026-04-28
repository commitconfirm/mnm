"""django-tables2 table definitions for plugin list views.

E1 ships ``EndpointTable``. Cross-link rendering uses the
cross-vendor naming helper (``utils/interface.py``) for the
``current_port`` column, ensuring a Junos slot/port form like
``ge-0/0/12`` resolves to the same Nautobot Interface as
``ge-0/0/12.0`` (logical-unit form).

Sentinel rows (``ifindex:N`` in ``current_port``) render with a
``badge-sentinel`` styled chip and a tooltip explaining the
fallback. They never link to an Interface page.
"""

from django.utils.html import format_html
from django.utils.safestring import mark_safe

import django_tables2 as tables

from nautobot.apps.tables import BaseTable, ToggleColumn

from mnm_plugin import models
from mnm_plugin.utils import interface as iface_utils


class EndpointTable(BaseTable):
    """Endpoint list view table."""

    pk = ToggleColumn()

    mac_address = tables.Column(
        linkify=True,
        verbose_name="MAC",
    )
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
        """Link to Nautobot Device when one matches by name."""
        if not value or value == "(none)":
            return value or "—"
        try:
            from nautobot.dcim.models import Device

            device = Device.objects.filter(name=value).first()
            if device:
                return format_html(
                    '<a href="{}">{}</a>',
                    device.get_absolute_url(),
                    value,
                )
        except Exception:  # noqa: BLE001
            pass
        return value

    def render_current_port(self, value, record):
        """Link to Nautobot Interface via cross-vendor naming
        helper. Sentinel rows render with a badge."""
        if not value or value == "(none)":
            return value or "—"
        if iface_utils.is_sentinel(value):
            return format_html(
                '<span class="badge badge-warning" '
                'title="ifindex resolution failed; raw bridge port shown">'
                '{}</span>',
                value,
            )
        iface = iface_utils.get_interface(record.current_switch, value)
        if iface:
            return format_html(
                '<a href="{}">{}</a>',
                iface.get_absolute_url(),
                value,
            )
        return value

    def render_current_ip(self, value, record):
        """Link to Nautobot IPAddress when one matches by host."""
        if not value:
            return "—"
        try:
            from nautobot.ipam.models import IPAddress

            ip = IPAddress.objects.filter(host=value).first()
            if ip:
                return format_html(
                    '<a href="{}">{}</a>',
                    ip.get_absolute_url(),
                    value,
                )
        except Exception:  # noqa: BLE001
            pass
        return value
