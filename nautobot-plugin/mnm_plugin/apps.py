"""Re-export of ``MnmPluginConfig`` for back-compat.

The canonical location is ``mnm_plugin.__init__`` — Nautobot's
plugin URL discovery requires the AppConfig to live in the
plugin package's top-level module so its ``__module__`` is the
package name. See ``mnm_plugin/__init__.py`` for the rationale
and the class definition.
"""

from mnm_plugin import MnmPluginConfig  # noqa: F401
