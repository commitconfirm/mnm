"""DRF serializers for plugin REST API."""

from nautobot.apps.api import NautobotModelSerializer

from mnm_plugin import models


class EndpointSerializer(NautobotModelSerializer):
    class Meta:
        model = models.Endpoint
        fields = "__all__"
