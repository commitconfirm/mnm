"""DRF serializers for plugin REST API.

E1 ships ``EndpointSerializer``. E2 adds three more for the
link-layer triad. ``fields = "__all__"`` for v1.0 — operators
get every model column over REST. E6 may trim the surface for
the saved-filter export path if performance dictates.
"""

from nautobot.apps.api import NautobotModelSerializer

from mnm_plugin import models


class EndpointSerializer(NautobotModelSerializer):
    class Meta:
        model = models.Endpoint
        fields = "__all__"


class ArpEntrySerializer(NautobotModelSerializer):
    class Meta:
        model = models.ArpEntry
        fields = "__all__"


class MacEntrySerializer(NautobotModelSerializer):
    class Meta:
        model = models.MacEntry
        fields = "__all__"


class LldpNeighborSerializer(NautobotModelSerializer):
    class Meta:
        model = models.LldpNeighbor
        fields = "__all__"


class RouteSerializer(NautobotModelSerializer):
    class Meta:
        model = models.Route
        fields = "__all__"


class BgpNeighborSerializer(NautobotModelSerializer):
    class Meta:
        model = models.BgpNeighbor
        fields = "__all__"


class FingerprintSerializer(NautobotModelSerializer):
    class Meta:
        model = models.Fingerprint
        fields = "__all__"
