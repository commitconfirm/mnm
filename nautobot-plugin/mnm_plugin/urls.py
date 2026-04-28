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
]
