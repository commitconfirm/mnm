"""Vendor probe modules for Phase 1 onboarding.

Each module exposes ``probe_device_facts(ip, snmp_community)`` returning a
``DeviceFacts`` dataclass (hostname / serial / chassis_model / os_version /
management_prefix_length). The orchestrator dispatches to the right module
based on the vendor returned by :mod:`app.onboarding.classifier`.

v1.0 ships the Junos probe (Prompt 4). Arista (Prompt 5), PAN-OS (Prompt 7),
and FortiGate (Prompt 7.5) follow.
"""
