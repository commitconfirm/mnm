"""CLI for validating the onboarding classifier against a real device.

This script exercises the same code path as
:func:`app.onboarding.classifier.classify` — collects sysDescr and
sysObjectID via SNMPv2c, runs the full classifier, and prints the
:class:`ClassifierResult` as JSON for diffing.

Not part of any production path. Operator validation / reality-check
reconciliation tool.

Usage (from inside the controller container):
    python -m app.scripts.classify_probe --ip <addr> --community <string>
    python -m app.scripts.classify_probe --ip <addr> --community <string> --ports 22/tcp,443/tcp

Exit codes:
    0 — classifier returned a result (even "unknown")
    1 — SNMP reachability failure (hard error before classification)
    2 — usage error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _run(ip: str, community: str, ports: list[str], mac_vendor: str) -> int:
    from app.onboarding.classifier import classify

    try:
        result = await classify(
            ip,
            community,
            ports_open=ports,
            mac_vendor=mac_vendor,
        )
    except Exception as exc:  # noqa: BLE001 — surfacing any unexpected error to operator
        print(f"ERROR: classifier raised {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Probe the onboarding classifier.")
    ap.add_argument("--ip", required=True, help="Target device IPv4 address")
    ap.add_argument("--community", required=True,
                    help="SNMPv2c community string (read-only)")
    ap.add_argument("--ports", default="",
                    help=("Comma-separated port list (e.g. 22/tcp,443/tcp). "
                          "Optional — forwarded to the classifier as the "
                          "ports_open signal."))
    ap.add_argument("--mac-vendor", default="",
                    help="Optional MAC-vendor string (OUI lookup result)")
    args = ap.parse_args()

    ports = [p.strip() for p in args.ports.split(",") if p.strip()]
    return asyncio.run(_run(args.ip, args.community, ports, args.mac_vendor))


if __name__ == "__main__":
    sys.exit(main())
