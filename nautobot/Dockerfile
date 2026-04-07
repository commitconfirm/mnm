FROM networktocode/nautobot:3.0-py3.11

# Install NAPALM drivers and device onboarding plugin
# napalm and junos-eznc are already in the base image but listed explicitly for clarity
RUN pip install --no-cache-dir \
    nautobot-device-onboarding \
    nautobot-welcome-wizard \
    napalm \
    junos-eznc

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

# Copy custom Nautobot configuration
COPY --chown=nautobot:nautobot nautobot_config.py /opt/nautobot/nautobot_config.py

# Ensure volume mount points exist with correct ownership
RUN mkdir -p /opt/nautobot/media /opt/nautobot/static && \
    chown -R nautobot:nautobot /opt/nautobot/media /opt/nautobot/static
