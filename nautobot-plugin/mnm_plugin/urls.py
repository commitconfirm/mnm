"""URL routes under the ``/plugins/mnm/`` namespace."""

from django.urls import path

from mnm_plugin import views


app_name = "mnm_plugin"

urlpatterns = [
    # Endpoint (E1)
    path(
        "endpoints/",
        views.EndpointListView.as_view(),
        name="endpoint_list",
    ),
    path(
        "endpoints/<uuid:pk>/",
        views.EndpointView.as_view(),
        name="endpoint",
    ),
    # ArpEntry (E2)
    path(
        "arp-entries/",
        views.ArpEntryListView.as_view(),
        name="arpentry_list",
    ),
    path(
        "arp-entries/<uuid:pk>/",
        views.ArpEntryView.as_view(),
        name="arpentry",
    ),
    # MacEntry (E2)
    path(
        "mac-entries/",
        views.MacEntryListView.as_view(),
        name="macentry_list",
    ),
    path(
        "mac-entries/<uuid:pk>/",
        views.MacEntryView.as_view(),
        name="macentry",
    ),
    # LldpNeighbor (E2)
    path(
        "lldp-neighbors/",
        views.LldpNeighborListView.as_view(),
        name="lldpneighbor_list",
    ),
    path(
        "lldp-neighbors/<uuid:pk>/",
        views.LldpNeighborView.as_view(),
        name="lldpneighbor",
    ),
    # Route (E3)
    path(
        "routes/",
        views.RouteListView.as_view(),
        name="route_list",
    ),
    path(
        "routes/<uuid:pk>/",
        views.RouteView.as_view(),
        name="route",
    ),
    # BgpNeighbor (E3)
    path(
        "bgp-neighbors/",
        views.BgpNeighborListView.as_view(),
        name="bgpneighbor_list",
    ),
    path(
        "bgp-neighbors/<uuid:pk>/",
        views.BgpNeighborView.as_view(),
        name="bgpneighbor",
    ),
    # Fingerprint (E3 — schema-only in v1.0)
    path(
        "fingerprints/",
        views.FingerprintListView.as_view(),
        name="fingerprint_list",
    ),
    path(
        "fingerprints/<uuid:pk>/",
        views.FingerprintView.as_view(),
        name="fingerprint",
    ),
]
