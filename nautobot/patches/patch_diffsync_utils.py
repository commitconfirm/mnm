#!/usr/bin/env python3
"""Patch nautobot-device-onboarding's diffsync_utils.py to tolerate missing
chassis serials in Sync Network Data runs.

Background:
    Some devices (factory-reset, virtual chassis members, mid-RMA, certain
    EX2300 states) return command-getter results without a usable 'serial'
    key. Upstream's diffsync_utils.generate_device_queryset_from_command_getter_result
    (a) raises KeyError 'serial' on the dict access, killing the entire job,
    and (b) AND-joins on (name, serial) so even if the dict access were
    softened, an empty serial would silently fail to match the existing
    Device record's real serial in Nautobot, dropping the device.

Two changes:
    1. device_data["serial"] -> device_data.get("serial") or ""
    2. fall back to hostname-only filter when no non-empty serials were
       collected, so existing Device records (which already have real
       serials in Nautobot from the original onboarding) still get matched.

Tracking: https://github.com/nautobot/nautobot-app-device-onboarding/issues
"""
import pathlib
import sys

OLD_APPEND = 'devices_to_sync_serial_numbers.append(device_data["serial"])'
NEW_APPEND = 'devices_to_sync_serial_numbers.append(device_data.get("serial") or "")'

OLD_FILTER = (
    "device_queryset = Device.objects.filter(name__in=devices_to_sync_hostnames).filter(\n"
    "        serial__in=devices_to_sync_serial_numbers\n"
    "    )"
)
NEW_FILTER = (
    "device_queryset = Device.objects.filter(name__in=devices_to_sync_hostnames)\n"
    "    _non_empty_serials = [s for s in devices_to_sync_serial_numbers if s]\n"
    "    if _non_empty_serials:\n"
    "        device_queryset = device_queryset.filter(serial__in=_non_empty_serials)"
)

patched = 0
for path in pathlib.Path("/opt/nautobot").rglob(
    "nautobot_device_onboarding/utils/diffsync_utils.py"
):
    src = path.read_text()
    new = src.replace(OLD_APPEND, NEW_APPEND).replace(OLD_FILTER, NEW_FILTER)
    if new == src:
        print(f"WARN: no replacements made in {path}", file=sys.stderr)
        continue
    path.write_text(new)
    print(f"patched {path}")
    patched += 1

if patched == 0:
    print("ERROR: diffsync_utils.py not found or already patched", file=sys.stderr)
    sys.exit(1)
