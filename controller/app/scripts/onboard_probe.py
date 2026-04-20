"""CLI for validating the Phase 1 onboarding orchestrator against a real device.

Exercises the same code path as the future Discover-UI onboarding entry
point will (Prompt 8). Use this for operator validation of the v1.0
direct-REST onboarding flow — Junos only in Prompt 4.

Usage (from inside the controller container):
    python -m app.scripts.onboard_probe \\
        --ip <addr> --community <community> \\
        --location-id <uuid> --secrets-group-id <uuid>

Exit codes:
    0 — OnboardingResult(success=True)
    1 — OnboardingResult(success=False); stderr has the error
    2 — usage error
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys


async def _run(
    ip: str,
    community: str,
    location_id: str,
    secrets_group_id: str,
) -> int:
    from app import db
    from app.onboarding.orchestrator import onboard_device

    # The orchestrator's Step G (polling seed) silently no-ops when the
    # controller DB isn't initialized — that's by-design for degraded
    # startup paths, but a standalone ``python -m`` invocation of this
    # script runs outside the FastAPI lifespan so the DB module never
    # gets its init_db() call. Initialise here before dispatching so
    # device_polls rows actually get seeded during live validation.
    await db.init_db()

    result = await onboard_device(
        ip=ip,
        snmp_community=community,
        secrets_group_id=secrets_group_id,
        location_id=location_id,
    )

    payload = {
        "success": result.success,
        "device_id": result.device_id,
        "device_name": result.device_name,
        "error": result.error,
        "rollback_performed": result.rollback_performed,
        "phase1_steps_completed": result.phase1_steps_completed,
        "classification": (
            result.classification.to_dict() if result.classification else None
        ),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))

    if not result.success:
        print(f"FAILED: {result.error}", file=sys.stderr)
        return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=("Phase 1 direct-REST onboarding probe (Junos only in "
                     "v1.0 Prompt 4)."),
    )
    ap.add_argument("--ip", required=True, help="Target device IPv4 address")
    ap.add_argument("--community", required=True,
                    help="SNMPv2c community string (read-only)")
    ap.add_argument("--location-id", required=True,
                    help="Nautobot Location UUID to place the device under")
    ap.add_argument("--secrets-group-id", required=True,
                    help="Nautobot SecretsGroup UUID for subsequent NAPALM/SSH use")
    args = ap.parse_args()

    sys.exit(asyncio.run(_run(
        args.ip, args.community, args.location_id, args.secrets_group_id,
    )))


if __name__ == "__main__":
    main()
