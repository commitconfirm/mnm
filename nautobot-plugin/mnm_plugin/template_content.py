"""Template content extensions registered with Nautobot.

E5 (interface-detail extension — the Rule 12 marquee feature)
adds the ``InterfaceMnmPanels`` ``TemplateExtension`` that hooks
into Nautobot's ``dcim.interface`` detail page and renders four
inline panels showing the MNM data for that port:

  - Endpoints currently on this port
  - Endpoints historically on this port (90-day window)
  - LLDP neighbors on this port
  - MAC entries on this port

Per E0 §3d + §7 Q5 the rendering posture is **inline panels**,
not a tab. ``right_page()`` is the Nautobot API for that — content
returned by ``right_page()`` lands inline in the right column of
the host detail page.

Auto-discovery: this file is at
``mnm_plugin/template_content.py`` and the variable name is
``template_extensions`` — both canonical Nautobot 3.x discovery
targets per E1 §10's lesson. No AppConfig override needed.

Fail-soft posture per E4: every panel's ORM query is wrapped in
a BLE001 guard. One panel raising never breaks the host
Interface detail page; the affected panel renders an error
notice and the others continue.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone

from nautobot.apps.ui import TemplateExtension


log = logging.getLogger(__name__)


# Cap each panel at the most-recent N rows. Above this, the panel
# footer renders a "Show all N rows" link to the relevant model's
# list view filtered on (device, interface). Same convention as
# E4 detail-page panels.
PANEL_LIMIT = 25

# Historical-Endpoint window. Endpoints older than this don't
# appear in the "historically on this port" panel by default —
# operators can still reach them via the "Show all" link to the
# unfiltered list view if needed.
HISTORICAL_WINDOW_DAYS = 90


def _safe_query(label: str, fn) -> tuple[Any, str | None]:
    """Run a panel's ORM query under a BLE001 guard.

    Returns ``(result, error_message)``. On success, ``error_message``
    is ``None`` and the panel renders normally. On failure, ``result``
    is ``None`` and the panel renders the error message in its body.
    Failure does not raise — the host page is preserved.
    """
    try:
        return fn(), None
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "interface_mnm_panel_failed | %s | %s", label, exc,
        )
        return None, f"Panel rendering failed ({label})."


class InterfaceMnmPanels(TemplateExtension):
    """Render MNM data panels on ``dcim.interface`` detail pages.

    Hooks into Nautobot's plugin extension point. ``self.context``
    is set by the parent class to the rendering context dict;
    ``self.context["object"]`` is the ``dcim.Interface`` instance.
    """

    model = "dcim.interface"

    def right_page(self) -> str:
        # Lazy imports — module-level imports of plugin models
        # would fail at template_content.py import time (Django
        # apps not yet loaded), since Nautobot imports plugin
        # template_content modules during app registry setup.
        from mnm_plugin.models import Endpoint, LldpNeighbor, MacEntry
        from mnm_plugin.utils.interface import expand_for_lookup

        interface = self.context.get("object")
        if interface is None or not getattr(interface, "device", None):
            return ""

        device_name = interface.device.name
        interface_name = interface.name
        candidates = expand_for_lookup(interface_name)
        if not candidates:
            return ""

        cutoff = timezone.now() - timedelta(days=HISTORICAL_WINDOW_DAYS)

        # Endpoints currently on this port — active rows.
        active_qs, active_err = _safe_query(
            "endpoint_active",
            lambda: list(
                Endpoint.objects
                .filter(
                    current_switch=device_name,
                    current_port__in=candidates,
                    active=True,
                )
                .order_by("-last_seen")[:PANEL_LIMIT]
            ),
        )

        # Endpoints historically on this port — 90-day window;
        # includes active rows by design (operator wants the full
        # timeline).
        history_qs, history_err = _safe_query(
            "endpoint_history",
            lambda: list(
                Endpoint.objects
                .filter(
                    current_switch=device_name,
                    current_port__in=candidates,
                    last_seen__gte=cutoff,
                )
                .order_by("-last_seen")[:PANEL_LIMIT]
            ),
        )

        # If the 90-day window is empty, check if there's anything
        # at all so the template can show the "Show endpoints beyond
        # 90 days →" footer link.
        history_has_older, _ = _safe_query(
            "endpoint_history_check",
            lambda: Endpoint.objects.filter(
                current_switch=device_name,
                current_port__in=candidates,
            ).exists(),
        )
        history_has_older = bool(history_has_older) if not history_err else False

        # LLDP neighbors on this port.
        lldp_qs, lldp_err = _safe_query(
            "lldp",
            lambda: list(
                LldpNeighbor.objects
                .filter(
                    node_name=device_name,
                    local_interface__in=candidates,
                )
                .order_by("-collected_at")[:PANEL_LIMIT]
            ),
        )

        # MAC entries on this port.
        mac_qs, mac_err = _safe_query(
            "mac",
            lambda: list(
                MacEntry.objects
                .filter(
                    node_name=device_name,
                    interface__in=candidates,
                )
                .order_by("-collected_at")[:PANEL_LIMIT]
            ),
        )

        # "Show all" links — built from the existing list-view URL
        # names (E1+E2+E3) with query-string filters that the
        # filtersets already support.
        endpoint_list_url = (
            f"{reverse('plugins:mnm_plugin:endpoint_list')}"
            f"?current_switch={device_name}&current_port={interface_name}"
        )
        lldp_list_url = (
            f"{reverse('plugins:mnm_plugin:lldpneighbor_list')}"
            f"?node_name={device_name}&local_interface={interface_name}"
        )
        mac_list_url = (
            f"{reverse('plugins:mnm_plugin:macentry_list')}"
            f"?node_name={device_name}&interface={interface_name}"
        )

        return render_to_string(
            "mnm_plugin/inc/interface_panels.html",
            {
                "device_name": device_name,
                "interface_name": interface_name,
                "active_endpoints": active_qs or [],
                "active_endpoints_err": active_err,
                "historical_endpoints": history_qs or [],
                "historical_endpoints_err": history_err,
                "historical_endpoints_has_older_than_window": (
                    history_has_older and not (history_qs or [])
                ),
                "historical_window_days": HISTORICAL_WINDOW_DAYS,
                "lldp_neighbors": lldp_qs or [],
                "lldp_neighbors_err": lldp_err,
                "mac_entries": mac_qs or [],
                "mac_entries_err": mac_err,
                "endpoint_list_url": endpoint_list_url,
                "lldp_list_url": lldp_list_url,
                "mac_list_url": mac_list_url,
                "panel_limit": PANEL_LIMIT,
            },
        )


template_extensions = [InterfaceMnmPanels]
