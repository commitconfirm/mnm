"""MNM Nautobot plugin.

See README.md for an overview, and docs/PLUGIN.md in the parent
mnm repo for the operator-facing guide.
"""

__version__ = "1.0.0a1"

# Nautobot's plugin loader reads ``config`` from the package
# top-level and expects an ``NautobotAppConfig`` *class* (not a
# string). The string form is a Django legacy that doesn't apply
# here.
from mnm_plugin.apps import MnmPluginConfig as config  # noqa: E402,F401
