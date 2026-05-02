"""REST API URL routes."""

from nautobot.apps.api import OrderedDefaultRouter

from mnm_plugin.api import views


router = OrderedDefaultRouter()

# Don't override ``APIRootView`` here — Nautobot's
# ``OrderedDefaultRouter.get_api_root_view`` does
# ``issubclass(self.APIRootView, AuthenticatedAPIRootView)`` and
# ``issubclass(None, ...)`` raises TypeError. Default class
# (Nautobot's authenticated root) is what we want.

router.register("endpoints", views.EndpointViewSet)
router.register("arp-entries", views.ArpEntryViewSet)
router.register("mac-entries", views.MacEntryViewSet)
router.register("lldp-neighbors", views.LldpNeighborViewSet)
router.register("routes", views.RouteViewSet)
router.register("bgp-neighbors", views.BgpNeighborViewSet)
router.register("fingerprints", views.FingerprintViewSet)

app_name = "mnm_plugin-api"

urlpatterns = router.urls
