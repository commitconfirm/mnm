"""Bulk import all manufacturers and device types from the Welcome Wizard library.

Run inside the Nautobot container via:
    cat import_devicetypes.py | nautobot-server nbshell

Idempotent — skips manufacturers and device types that already exist.
Writes results to /tmp/import_results.json for the bootstrap script to read.
"""

import json
import sys
import traceback

from django.db import IntegrityError

from nautobot.dcim.forms import DeviceTypeImportForm
from nautobot.dcim.models import (
    ConsolePortTemplate,
    ConsoleServerPortTemplate,
    DeviceBayTemplate,
    DeviceType,
    FrontPortTemplate,
    InterfaceTemplate,
    Manufacturer,
    ModuleBayTemplate,
    PowerOutletTemplate,
    PowerPortTemplate,
    RearPortTemplate,
)
from welcome_wizard.models.importer import DeviceTypeImport, ManufacturerImport

# Component mapping (same as welcome_wizard/jobs.py)
COMPONENTS = {
    "console-ports": ConsolePortTemplate,
    "console-server-ports": ConsoleServerPortTemplate,
    "power-ports": PowerPortTemplate,
    "power-outlets": PowerOutletTemplate,
    "interfaces": InterfaceTemplate,
    "rear-ports": RearPortTemplate,
    "front-ports": FrontPortTemplate,
    "device-bays": DeviceBayTemplate,
    "module-bays": ModuleBayTemplate,
}

# Fields to strip from component data (community library uses NetBox field
# names that don't exist in Nautobot's component templates)
STRIP_KEYWORDS = {
    "interfaces": ["poe_mode", "poe_type", "enabled"],
    "power-outlets": ["power_port"],
    "front-ports": ["rear_port"],
    "rear-ports": ["positions"],
}

results = {
    "mfg_created": 0,
    "mfg_skipped": 0,
    "dt_created": 0,
    "dt_skipped": 0,
    "dt_failed": 0,
    "errors": [],
}

# ---------------------------------------------------------------------------
# 1. Import all manufacturers
# ---------------------------------------------------------------------------
wizard_manufacturers = ManufacturerImport.objects.values_list("name", flat=True)
for name in wizard_manufacturers:
    _, created = Manufacturer.objects.get_or_create(name=name)
    if created:
        results["mfg_created"] += 1
    else:
        results["mfg_skipped"] += 1

# ---------------------------------------------------------------------------
# 2. Import all device types
# ---------------------------------------------------------------------------
wizard_device_types = DeviceTypeImport.objects.select_related("manufacturer").all()
for wdt in wizard_device_types:
    data = wdt.device_type_data
    mfg_name = data.get("manufacturer", "")
    model = data.get("model", "")

    # Skip if already exists
    try:
        mfg = Manufacturer.objects.get(name=mfg_name)
    except Manufacturer.DoesNotExist:
        results["dt_failed"] += 1
        results["errors"].append(f"Manufacturer '{mfg_name}' not found for {model}")
        continue

    if DeviceType.objects.filter(model=model, manufacturer=mfg).exists():
        results["dt_skipped"] += 1
        continue

    try:
        dtif = DeviceTypeImportForm(data)
        if not dtif.is_valid():
            results["dt_failed"] += 1
            if len(results["errors"]) < 20:
                results["errors"].append(f"{mfg_name}/{model}: {dtif.errors}")
            continue

        devtype = dtif.save()

        # Import component templates
        for key, component_class in COMPONENTS.items():
            if key in data:
                component_list = [
                    component_class(
                        device_type=devtype,
                        **{k: v for k, v in item.items() if k not in STRIP_KEYWORDS.get(key, [])},
                    )
                    for item in data[key]
                ]
                component_class.objects.bulk_create(component_list)

        results["dt_created"] += 1
    except (IntegrityError, ValueError, Exception) as exc:
        results["dt_failed"] += 1
        if len(results["errors"]) < 20:
            results["errors"].append(f"{mfg_name}/{model}: {exc}")

# Write results
with open("/tmp/import_results.json", "w") as f:
    json.dump(results, f)
