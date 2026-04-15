FROM networktocode/nautobot:3.0-py3.11

# Install NAPALM drivers and device onboarding plugin
# napalm and junos-eznc are already in the base image but listed explicitly for clarity
# Install NAPALM core + community drivers for all supported vendors.
# Base image includes napalm core (eos, ios, iosxr, junos, nxos).
# Community drivers: napalm-panos (Palo Alto) is well-maintained.
# Fortinet/MikroTik/Huawei community drivers have NAPALM 5.x compat issues —
# shim packages are created below so the onboarding plugin can load a driver.
RUN pip install --no-cache-dir \
    nautobot-device-onboarding \
    nautobot-welcome-wizard \
    napalm \
    junos-eznc \
    napalm-panos

# Shim packages for vendors whose community NAPALM drivers don't build on
# NAPALM 5.x. These wrap the ios driver so the onboarding plugin's
# get_network_driver() call doesn't crash. Netmiko handles the actual SSH
# connection using the correct device type (fortinet, mikrotik, huawei).
# Long-term: replace with vendor REST API connectors.
USER root
RUN mkdir -p /usr/local/lib/python3.11/site-packages/napalm_fortios \
             /usr/local/lib/python3.11/site-packages/napalm_ros \
             /usr/local/lib/python3.11/site-packages/napalm_ce && \
    echo 'from napalm.ios import IOSDriver as FortiOSDriver' > /usr/local/lib/python3.11/site-packages/napalm_fortios/__init__.py && \
    echo 'from napalm.ios import IOSDriver as ROSDriver' > /usr/local/lib/python3.11/site-packages/napalm_ros/__init__.py && \
    echo 'from napalm.ios import IOSDriver as CEDriver' > /usr/local/lib/python3.11/site-packages/napalm_ce/__init__.py

# Patch 5: Extend NETMIKO_TO_NAPALM_STATIC with vendor mappings missing from
# the upstream plugin. Without these, SSHDetect identifies the device type
# (e.g., "fortinet") but the plugin can't find the corresponding NAPALM driver.
RUN CONSTANTS=$(find /opt/nautobot -path "*/nautobot_device_onboarding/constants.py" -print -quit) && \
    if [ -n "$CONSTANTS" ]; then \
        echo '' >> "$CONSTANTS" && \
        echo '# MNM Patch 5: additional Netmiko-to-NAPALM mappings' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["fortinet"] = "fortios"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["fortinet_ssh"] = "fortios"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["mikrotik_routeros"] = "ros"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["huawei"] = "ce"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["paloalto_panos"] = "panos"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["aruba_osswitch"] = "ios"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["extreme_exos"] = "ios"' >> "$CONSTANTS" && \
        echo 'NETMIKO_TO_NAPALM_STATIC["ubiquiti_edgerouter"] = "ios"' >> "$CONSTANTS"; \
    fi && \
    find /opt/nautobot -path "*/nautobot_device_onboarding/__pycache__" -exec rm -rf {} + 2>/dev/null || true
USER nautobot

# Patch 1: Increase Netmiko read_timeout for large Juniper configs.
# The plugin hardcodes 60s which isn't enough for 'show configuration interfaces'.
# Patch 2: Default port to 22 when not set on device.
# Without this, 'Sync Network Data' fails with int(None) on tcp_ping
# because NautobotORMInventory doesn't set Host.port and the onboarding
# plugin's command_getter.py uses task.host.port directly.
RUN find /opt/nautobot -path "*/nautobot_device_onboarding/nornir_plays/command_getter.py" \
        -exec sed -i \
            -e 's/read_timeout=60/read_timeout=120/' \
            -e 's/tcp_ping(task.host.hostname, task.host.port)/tcp_ping(task.host.hostname, task.host.port or 22)/' \
        {} + && \
    find /opt/nautobot -path "*/nautobot_device_onboarding/nornir_plays/__pycache__" \
        -exec rm -rf {} + 2>/dev/null || true

# Patch 3: Tolerate missing chassis serial in Sync Network Data.
# Some devices (factory-reset, virtual chassis members, mid-RMA, certain
# EX2300 states) return command-getter results without a usable 'serial'
# key. Upstream's diffsync_utils.generate_device_queryset_from_command_getter_result
# (a) raises KeyError 'serial' on the dict access, killing the entire job,
# and (b) AND-joins the queryset on (name, serial) so even with the dict
# access softened, an empty serial would silently fail to match the
# existing Device record's real serial in Nautobot.
#
# The full patch is a Python script (multi-line replacement is awkward in
# sed); see patches/patch_diffsync_utils.py for the rationale and the exact
# replacement text.
#
# Tracking: https://github.com/nautobot/nautobot-app-device-onboarding/issues
COPY patches/patch_diffsync_utils.py /tmp/patch_diffsync_utils.py
RUN python3 /tmp/patch_diffsync_utils.py && \
    rm /tmp/patch_diffsync_utils.py && \
    find /opt/nautobot -path "*/nautobot_device_onboarding/utils/__pycache__" \
        -exec rm -rf {} + 2>/dev/null || true

# Patch 4: Always surface schema-validation errors from the Sync jobs.
# Upstream logs the actual jsonschema ValidationError at DEBUG level only,
# which means the operator only ever sees the generic
# "Schema validation failed." message and has no way to know which field
# is missing without re-running the job in debug mode. Promote those two
# log calls to WARNING so the actionable detail is always recorded in the
# JobResult log without operator action. Surgical sed: only the two
# Schema-validation lines change; other DEBUG logging is untouched.
COPY patches/patch_processor_schema_logging.py /tmp/patch_processor_schema_logging.py
RUN python3 /tmp/patch_processor_schema_logging.py && \
    rm /tmp/patch_processor_schema_logging.py && \
    find /opt/nautobot -path "*/nautobot_device_onboarding/nornir_plays/__pycache__" \
        -exec rm -rf {} + 2>/dev/null || true

# Copy custom Nautobot configuration
COPY --chown=nautobot:nautobot nautobot_config.py /opt/nautobot/nautobot_config.py

# Ensure volume mount points exist with correct ownership
RUN mkdir -p /opt/nautobot/media /opt/nautobot/static && \
    chown -R nautobot:nautobot /opt/nautobot/media /opt/nautobot/static
