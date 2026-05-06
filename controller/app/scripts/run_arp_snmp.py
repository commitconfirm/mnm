"""CLI for validating the SNMP ARP collector against a real device.

This script is NOT part of the polling loop or any production path.
It exists solely for operator validation and debugging of arp_snmp.collect_arp().

Usage (from inside the controller container):
    python -m app.scripts.run_arp_snmp --device <ip> --community <string>
    python -m app.scripts.run_arp_snmp --device <ip> --community <string> --timeout 15

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


async def _run(device: str, community: str, timeout: float) -> int:
    from app.arp_snmp import collect_arp
    from app.snmp_collector import SnmpAuthError, SnmpError, SnmpTimeoutError

    print(f"Collecting ARP table from {device} via SNMPv2c ...")
    t0 = time.monotonic()
    try:
        entries = await collect_arp(device, community, timeout_sec=timeout)
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

    print(f"\nResults: {len(entries)} entries in {elapsed_ms} ms\n")

    if not entries:
        print("(no ARP entries returned)")
        return 0

    col_w = [16, 19, 10, 8]
    header = (
        f"{'IP Address':<{col_w[0]}}  "
        f"{'MAC Address':<{col_w[1]}}  "
        f"{'ifIndex':<{col_w[2]}}  "
        f"{'Type':<{col_w[3]}}"
    )
    sep = "  ".join("-" * w for w in col_w)
    print(header)
    print(sep)
    for e in sorted(entries, key=lambda x: x.ip_address):
        print(
            f"{e.ip_address:<{col_w[0]}}  "
            f"{e.mac_address:<{col_w[1]}}  "
            f"{e.interface_index:<{col_w[2]}}  "
            f"{e.entry_type:<{col_w[3]}}"
        )

    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Validate SNMP ARP collection against a real device."
    )
    parser.add_argument("--device", required=True, help="Device IP address")
    parser.add_argument("--community", required=True, help="SNMP community string")
    parser.add_argument("--timeout", type=float, default=10.0,
                        help="Per-PDU timeout in seconds (default: 10)")
    args = parser.parse_args()

    sys.exit(asyncio.run(_run(args.device, args.community, args.timeout)))


if __name__ == "__main__":
    main()
