"""CLI for validating the SNMP MAC/FDB collector against a real device.

This script is NOT part of the polling loop or any production path.
It exists solely for operator validation and debugging of mac_snmp.collect_mac().

Usage (from inside the controller container):
    python -m app.scripts.run_mac_snmp --device <ip> --community <string>
    python -m app.scripts.run_mac_snmp --device <ip> --community <string> --timeout 15

Output groups entries by VLAN when the primary (VLAN-aware) table is used.
When the BRIDGE-MIB fallback is used, entries are shown in a flat table.

Exit codes:
    0 — collection succeeded (zero entries is still success)
    1 — SNMP error (timeout, auth failure, or protocol error)
    2 — usage error
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from collections import defaultdict


async def _run(device: str, community: str, timeout: float) -> int:
    from app.mac_snmp import collect_mac
    from app.snmp_collector import SnmpAuthError, SnmpError, SnmpTimeoutError

    print(f"Collecting MAC/FDB table from {device} via SNMPv2c ...")
    t0 = time.monotonic()
    try:
        entries = await collect_mac(device, community, timeout_sec=timeout)
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
    vlan_aware = any(e.vlan is not None for e in entries)
    table_src = "dot1qTpFdbTable (VLAN-aware)" if vlan_aware else "dot1dTpFdbTable (BRIDGE-MIB fallback)"

    print(f"\nResults: {len(entries)} entries in {elapsed_ms} ms  [{table_src}]\n")

    if not entries:
        print("(no MAC/FDB entries returned — device may not implement bridging MIBs)")
        return 0

    col_w = [19, 6, 8]
    header = (
        f"  {'MAC Address':<{col_w[0]}}  "
        f"{'Port':<{col_w[1]}}  "
        f"{'Status':<{col_w[2]}}"
    )
    sep = "  " + "  ".join("-" * w for w in col_w)

    if vlan_aware:
        # Group by VLAN
        by_vlan: dict[int, list] = defaultdict(list)
        for e in entries:
            by_vlan[e.vlan].append(e)  # type: ignore[index]

        for vlan in sorted(by_vlan.keys()):
            vlan_entries = sorted(by_vlan[vlan], key=lambda x: x.mac_address)
            print(f"VLAN {vlan} ({len(vlan_entries)} entries):")
            print(header)
            print(sep)
            for e in vlan_entries:
                print(
                    f"  {e.mac_address:<{col_w[0]}}  "
                    f"{e.bridge_port:<{col_w[1]}}  "
                    f"{e.entry_status:<{col_w[2]}}"
                )
            print()
    else:
        # Flat table, no VLAN column
        print(header)
        print(sep)
        for e in sorted(entries, key=lambda x: x.mac_address):
            print(
                f"  {e.mac_address:<{col_w[0]}}  "
                f"{e.bridge_port:<{col_w[1]}}  "
                f"{e.entry_status:<{col_w[2]}}"
            )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate SNMP MAC/FDB collection against a real device."
    )
    parser.add_argument("--device", required=True, help="Device IP address")
    parser.add_argument("--community", required=True, help="SNMP community string")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Per-PDU timeout in seconds (default: 10)")
    args = parser.parse_args()

    sys.exit(asyncio.run(_run(args.device, args.community, args.timeout)))


if __name__ == "__main__":
    main()
