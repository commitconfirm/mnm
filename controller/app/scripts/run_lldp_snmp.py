"""CLI for validating the SNMP LLDP collector against a real device.

This script is NOT part of the polling loop or any production path.
It exists solely for operator validation and debugging of
lldp_snmp.collect_lldp().

Usage (from inside the controller container):
    python -m app.scripts.run_lldp_snmp --device <ip> --community <string>
    python -m app.scripts.run_lldp_snmp --device <ip> --community <string> --timeout 15

Output is a table of neighbors with local_port, chassis_id, port_id,
system name, and management IP. A summary line reports how many
neighbors had a management IP populated vs None.

Exit codes:
    0 — collection succeeded (zero neighbors is still success)
    1 — SNMP error (timeout, auth failure, or protocol error)
    2 — usage error
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time


async def _run(device: str, community: str, timeout: float) -> int:
    from app.lldp_snmp import collect_lldp
    from app.snmp_collector import SnmpAuthError, SnmpError, SnmpTimeoutError

    print(f"Collecting LLDP neighbors from {device} via SNMPv2c ...")
    t0 = time.monotonic()
    try:
        neighbors = await collect_lldp(device, community, timeout_sec=timeout)
    except SnmpTimeoutError as exc:
        print(f"ERROR: timeout — {exc}", file=sys.stderr)
        return 1
    except SnmpAuthError as exc:
        print(f"ERROR: authentication failure — {exc}", file=sys.stderr)
        return 1
    except SnmpError as exc:
        print(f"ERROR: SNMP error — {exc}", file=sys.stderr)
        return 1

    elapsed_ms = round((time.monotonic() - t0) * 1000, 1)

    with_mgmt = sum(1 for n in neighbors if n.management_ip is not None)
    without_mgmt = len(neighbors) - with_mgmt

    print(f"\nResults: {len(neighbors)} neighbors in {elapsed_ms} ms")
    print(f"  with management IP: {with_mgmt}")
    print(f"  without management IP: {without_mgmt}\n")

    if not neighbors:
        print("(no LLDP neighbors returned — device may have LLDP disabled,"
              " or no peer is sending LLDPDUs on any local port)")
        return 0

    col_w = [16, 20, 22, 22, 20, 16]
    header = (
        f"  {'Local Port':<{col_w[0]}}  "
        f"{'Chassis ID':<{col_w[1]}}  "
        f"{'Chassis Subtype':<{col_w[2]}}  "
        f"{'Port ID':<{col_w[3]}}  "
        f"{'System':<{col_w[4]}}  "
        f"{'Management IP':<{col_w[5]}}"
    )
    sep = "  " + "  ".join("-" * w for w in col_w)

    print(header)
    print(sep)

    sort_key = lambda n: (n.local_port_name or "", n.local_port_ifindex)
    for n in sorted(neighbors, key=sort_key):
        local = n.local_port_name or f"ifIndex:{n.local_port_ifindex}"
        sysname = n.remote_system_name or "(none)"
        mgmt = n.management_ip or "-"
        print(
            f"  {local:<{col_w[0]}}  "
            f"{n.remote_chassis_id:<{col_w[1]}}  "
            f"{n.remote_chassis_id_subtype:<{col_w[2]}}  "
            f"{n.remote_port_id:<{col_w[3]}}  "
            f"{sysname:<{col_w[4]}}  "
            f"{mgmt:<{col_w[5]}}"
        )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate SNMP LLDP collection against a real device."
    )
    parser.add_argument("--device", required=True, help="Device IP address")
    parser.add_argument("--community", required=True, help="SNMP community string")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Per-PDU timeout in seconds (default: 10)")
    args = parser.parse_args()

    sys.exit(asyncio.run(_run(args.device, args.community, args.timeout)))


if __name__ == "__main__":
    main()
