"""URL routes under the ``/plugins/mnm/`` namespace."""

from django.urls import path

from mnm_plugin import views


app_name = "mnm_plugin"

urlpatterns = [
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
]
