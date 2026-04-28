"""NautobotAppConfig declaration for mnm-plugin.

The plugin registers under the URL namespace ``/plugins/mnm/`` (and
``/api/plugins/mnm/`` for REST). The Nautobot version pin is
``>=3.0,<3.1`` per E0 design Q7 — bump deliberately, not implicitly.
"""

from nautobot.apps import NautobotAppConfig


class MnmPluginConfig(NautobotAppConfig):
    name = "mnm_plugin"
    verbose_name = "MNM"
    description = (
        "Modular Network Monitor plugin: Endpoint, ARP, MAC, LLDP, "
        "Route, BGP, and Fingerprint models with Netdisco-style "
        "list views, plus an interface-detail extension."
    )
    # Hardcoded here (not imported from mnm_plugin.__init__) to
    # avoid the AppConfig-during-Nautobot-bootstrap cross-import
    # ordering trap.
    version = "1.0.0a1"
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
