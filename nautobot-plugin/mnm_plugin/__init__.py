"""MNM Nautobot plugin.

The ``NautobotAppConfig`` MUST live here (not in ``apps.py``)
because Nautobot's plugin URL discovery uses
``f"{config.__module__}.urls.urlpatterns"`` to find the
plugin's URL routes — so the AppConfig's ``__module__`` has to
be the plugin's top-level package, not a submodule. This
matches the convention every working Nautobot 3.x plugin
follows (see ``welcome_wizard``, ``nautobot_ssot``).

See README.md for an overview, and docs/PLUGIN.md in the parent
mnm repo for the operator-facing guide.
"""

__version__ = "1.0.0a1"

from nautobot.apps import NautobotAppConfig


class MnmPluginConfig(NautobotAppConfig):
    name = "mnm_plugin"
    verbose_name = "MNM"
    description = (
        "Modular Network Monitor plugin: Endpoint, ARP, MAC, LLDP, "
        "Route, BGP, and Fingerprint models with Netdisco-style "
        "list views, plus an interface-detail extension."
    )
    version = __version__
    author = "Jimmy (commitconfirm)"
    author_email = ""
    base_url = "mnm"

    # Pinned Nautobot version range per E0 §7 Q7. Re-validate on
    # Nautobot minor bumps; major bumps need a plugin release
    # cycle.
    min_version = "3.0.0"
    max_version = "3.0.999"

    required_settings = []
    default_settings = {}

    # Nautobot's default template-extensions discovery path is
    # ``<plugin>.template_content.template_extensions`` — no
    # explicit override needed at the AppConfig level. E5
    # populates ``template_content.py`` with the
    # interface-detail TemplateExtension subclasses.


config = MnmPluginConfig
