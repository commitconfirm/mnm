"""Connector framework for MNM (Phase 2.7+).

Each connector is a read-only client for an external system (hypervisor,
cloud controller, vendor API). Connectors collect inventory and metrics on
a schedule, optionally upsert MAC-keyed endpoints into the controller's
endpoint store, and expose Prometheus metrics for Grafana dashboards.

Inviolable Rule 1 applies: connectors NEVER write configuration to the
remote system. All API calls are GET / read-only.
"""
