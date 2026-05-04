"""URL routes under the ``/plugins/mnm/`` namespace."""

from functools import partial

from django.urls import path

from mnm_plugin import exports, views


app_name = "mnm_plugin"


# (url-slug, list-view-name, list-view-class, detail-view-name,
# detail-view-class) per model. Drives both the list/detail
# routes and the E6 export routes (CSV + JSON per model) below.
_MODELS = [
    ("endpoints", "endpoint", views.EndpointListView, views.EndpointView),
    ("arp-entries", "arpentry", views.ArpEntryListView, views.ArpEntryView),
    ("mac-entries", "macentry", views.MacEntryListView, views.MacEntryView),
    ("lldp-neighbors", "lldpneighbor", views.LldpNeighborListView, views.LldpNeighborView),
    ("routes", "route", views.RouteListView, views.RouteView),
    ("bgp-neighbors", "bgpneighbor", views.BgpNeighborListView, views.BgpNeighborView),
    ("fingerprints", "fingerprint", views.FingerprintListView, views.FingerprintView),
]


urlpatterns = []

for slug, name, list_cls, detail_cls in _MODELS:
    urlpatterns += [
        path(
            f"{slug}/",
            list_cls.as_view(),
            name=f"{name}_list",
        ),
        path(
            f"{slug}/<uuid:pk>/",
            detail_cls.as_view(),
            name=name,
        ),
        # E6: CSV + JSON export of filtered queryset.
        path(
            f"{slug}/export.csv",
            partial(exports.export_csv, model_key=slug),
            name=f"{name}_export_csv",
        ),
        path(
            f"{slug}/export.json",
            partial(exports.export_json, model_key=slug),
            name=f"{name}_export_json",
        ),
    ]
