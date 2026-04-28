"""REST API ViewSets."""

from nautobot.apps.api import NautobotModelViewSet

from mnm_plugin import filters, models
from mnm_plugin.api.serializers import EndpointSerializer


class EndpointViewSet(NautobotModelViewSet):
    queryset = models.Endpoint.objects.all()
    serializer_class = EndpointSerializer
    filterset_class = filters.EndpointFilterSet
