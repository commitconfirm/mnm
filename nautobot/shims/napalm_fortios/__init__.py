"""Shim: wraps ios driver so onboarding plugin can load 'fortios'."""
from napalm.ios import IOSDriver as FortiOSDriver  # noqa: F401
