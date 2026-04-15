"""Shim: wraps ios driver so onboarding plugin can load 'ros'."""
from napalm.ios import IOSDriver as ROSDriver  # noqa: F401
