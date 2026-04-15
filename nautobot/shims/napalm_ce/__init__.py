"""Shim: wraps ios driver so onboarding plugin can load 'ce'."""
from napalm.ios import IOSDriver as CEDriver  # noqa: F401
