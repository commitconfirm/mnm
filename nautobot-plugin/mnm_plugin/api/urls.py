"""REST API URL routes."""

from nautobot.apps.api import OrderedDefaultRouter

from mnm_plugin.api import views


router = OrderedDefaultRouter()
router.APIRootView = None  # use Nautobot's default API root view

router.register("endpoints", views.EndpointViewSet)

app_name = "mnm_plugin-api"

urlpatterns = router.urls
