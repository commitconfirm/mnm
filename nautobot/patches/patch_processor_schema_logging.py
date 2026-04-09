"""Patch nautobot-device-onboarding processor.py to surface schema errors.

Upstream logs jsonschema ValidationError detail at DEBUG level only,
behind a ``if self.job.debug:`` gate. The operator only ever sees the
generic "Schema validation failed." message in the JobResult log and
has no way to know which field is missing without re-running the job
with debug enabled.

This patch:
  1. Removes the ``self.job.debug`` gate around the two Schema validation
     blocks (both in ``subtask_instance_completed``).
  2. Promotes the inner ``self.logger.debug(...)`` calls that contain
     the ValidationError text to ``self.logger.warning(...)``.

Result: every Schema validation failure is logged at WARNING with the
exact missing/invalid field, visible in the JobResult log immediately.

The patch is idempotent — running it twice is a no-op.
"""

from __future__ import annotations

import glob
import sys

CANDIDATES = glob.glob(
    "/opt/nautobot/.local/lib/python*/site-packages/"
    "nautobot_device_onboarding/nornir_plays/processor.py"
)
if not CANDIDATES:
    print("patch_processor_schema_logging: target file not found", file=sys.stderr)
    sys.exit(1)

target = CANDIDATES[0]
with open(target, "r", encoding="utf-8") as fh:
    src = fh.read()

original = src

# 1. Promote the two debug() lines that emit the ValidationError to warning()
src = src.replace(
    'self.logger.debug(f"Schema validation failed for {host.name}. Error: {e}.")',
    'self.logger.warning(f"Schema validation failed for {host.name}. Error: {e}.")',
)
src = src.replace(
    'self.logger.debug(f"Schema validation failed for {host.name} Error: {err}")',
    'self.logger.warning(f"Schema validation failed for {host.name} Error: {err}")',
)

# 2. Remove the `if self.job.debug:` guard ONLY for those two blocks. The
#    pattern is unique enough — the line above the guarded warning() call
#    starts with "if self.job.debug:" and the next line is the (now)
#    warning() call. Strip the guard so the warning is always emitted.
src = src.replace(
    '                    if self.job.debug:\n'
    '                        self.logger.warning(f"Schema validation failed for {host.name}. Error: {e}.")',
    '                    self.logger.warning(f"Schema validation failed for {host.name}. Error: {e}.")',
)
src = src.replace(
    '                    if self.job.debug:\n'
    '                        self.logger.warning(f"Schema validation failed for {host.name} Error: {err}")',
    '                    self.logger.warning(f"Schema validation failed for {host.name} Error: {err}")',
)

if src == original:
    print("patch_processor_schema_logging: no changes (already patched or upstream changed)")
    sys.exit(0)

with open(target, "w", encoding="utf-8") as fh:
    fh.write(src)
print(f"patch_processor_schema_logging: patched {target}")
