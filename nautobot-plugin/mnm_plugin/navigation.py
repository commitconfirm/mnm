"""Nautobot navigation menu items.

Adds an "MNM" top-level group with the four current model
list-views as items. E3 (Routes/BGP/Fingerprint) extends this;
E5 may surface the interface-detail extension as a navigation
hint.
"""

from nautobot.apps.ui import NavMenuGroup, NavMenuItem, NavMenuTab


menu_items = (
    NavMenuTab(
        name="MNM",
        weight=1000,
        groups=(
            NavMenuGroup(
                name="Network State",
                weight=100,
                items=(
                    NavMenuItem(
                        link="plugins:mnm_plugin:endpoint_list",
                        name="Endpoints",
                        permissions=["mnm_plugin.view_endpoint"],
                    ),
                    NavMenuItem(
                        link="plugins:mnm_plugin:arpentry_list",
                        name="ARP Entries",
                        permissions=["mnm_plugin.view_arpentry"],
                    ),
                    NavMenuItem(
                        link="plugins:mnm_plugin:macentry_list",
                        name="MAC Entries",
                        permissions=["mnm_plugin.view_macentry"],
                    ),
                    NavMenuItem(
                        link="plugins:mnm_plugin:lldpneighbor_list",
                        name="LLDP Neighbors",
                        permissions=["mnm_plugin.view_lldpneighbor"],
                    ),
                    NavMenuItem(
                        link="plugins:mnm_plugin:route_list",
                        name="Routes",
                        permissions=["mnm_plugin.view_route"],
                    ),
                    NavMenuItem(
                        link="plugins:mnm_plugin:bgpneighbor_list",
                        name="BGP Neighbors",
                        permissions=["mnm_plugin.view_bgpneighbor"],
                    ),
                    NavMenuItem(
                        link="plugins:mnm_plugin:fingerprint_list",
                        name="Fingerprints",
                        permissions=["mnm_plugin.view_fingerprint"],
                    ),
                ),
            ),
        ),
    ),
)
