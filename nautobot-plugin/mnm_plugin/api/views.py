"""REST API ViewSets."""

from nautobot.apps.api import NautobotModelViewSet

from mnm_plugin import filters, models
from mnm_plugin.api.serializers import (
    ArpEntrySerializer,
    BgpNeighborSerializer,
    EndpointSerializer,
    FingerprintSerializer,
    LldpNeighborSerializer,
    MacEntrySerializer,
    RouteSerializer,
)


class EndpointViewSet(NautobotModelViewSet):
    queryset = models.Endpoint.objects.all()
    serializer_class = EndpointSerializer
    filterset_class = filters.EndpointFilterSet


class ArpEntryViewSet(NautobotModelViewSet):
    queryset = models.ArpEntry.objects.all()
    serializer_class = ArpEntrySerializer
    filterset_class = filters.ArpEntryFilterSet


class MacEntryViewSet(NautobotModelViewSet):
    queryset = models.MacEntry.objects.all()
    serializer_class = MacEntrySerializer
    filterset_class = filters.MacEntryFilterSet


class LldpNeighborViewSet(NautobotModelViewSet):
    queryset = models.LldpNeighbor.objects.all()
    serializer_class = LldpNeighborSerializer
    filterset_class = filters.LldpNeighborFilterSet


class RouteViewSet(NautobotModelViewSet):
    queryset = models.Route.objects.all()
    serializer_class = RouteSerializer
    filterset_class = filters.RouteFilterSet


class BgpNeighborViewSet(NautobotModelViewSet):
    queryset = models.BgpNeighbor.objects.all()
    serializer_class = BgpNeighborSerializer
    filterset_class = filters.BgpNeighborFilterSet


class FingerprintViewSet(NautobotModelViewSet):
    queryset = models.Fingerprint.objects.all()
    serializer_class = FingerprintSerializer
    filterset_class = filters.FingerprintFilterSet
