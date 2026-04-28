# mnm-plugin

A Nautobot Django app that adds the data models Nautobot lacks for
network-state inspection — Endpoint, ARP entry, MAC entry, LLDP
neighbor, route, BGP neighbor, and fingerprint records — and exposes
them as first-class Nautobot views.

This app is part of the **Modular Network Monitor (MNM)** project. It
fulfils MNM's "Documentation Is a Primary Output" architectural rule
by making Nautobot the operator-facing surface for inspecting
collected network state.

## Status

v1.0 (Block E in the MNM v1.0 roadmap). E1 ships:

- Plugin scaffold with `NautobotAppConfig`
- `Endpoint` model + list view + detail view + REST API
- Cross-vendor interface naming helper (`mnm_plugin.utils.interface`)

E2–E6 add the remaining models, the interface-detail extension, and
the filter framework. See `docs/PLUGIN.md` in the parent repo for the
operator-facing guide and `mnm-dev-claude:design/mnm_plugin_design.md`
for architectural background.

## Installation

Installed automatically when you bring up the MNM docker-compose
stack — see the parent repo's `docs/DEPLOYMENT.md`. Standalone
installs are out of scope for v1.0; the plugin co-versions with the
MNM controller.

## License

MIT. See parent repo's `LICENSE` file.
