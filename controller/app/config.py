"""Controller config persistence.

Reads/writes a single `controller_config` row in the kv_config table when
Postgres is available, falling back to /data/config.json when it isn't.
"""

import json
import os
from pathlib import Path

from sqlalchemy import select

from app import db
from app.logging_config import StructuredLogger

log = StructuredLogger(__name__, module="config")

DATA_DIR = Path(os.environ.get("MNM_DATA_DIR", "/data"))
CONFIG_PATH = DATA_DIR / "config.json"
ENDPOINTS_PATH = DATA_DIR / "endpoints.json"

CONFIG_KEY = "controller_config"

DEFAULT_CONFIG = {
    "setup_complete": False,
    "discovery_ranges": [],
    "sweep_schedules": [],
}

# In-process cache so synchronous code paths can read config without an event loop.
_cache: dict | None = None


def _load_json() -> dict:
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_CONFIG.copy()


def _write_json(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


async def load_config_async() -> dict:
    """Async load from Postgres (or JSON fallback)."""
    global _cache
    if db.is_ready():
        try:
            async with db.SessionLocal() as session:
                row = (await session.execute(
                    select(db.KVConfig).where(db.KVConfig.key == CONFIG_KEY)
                )).scalar_one_or_none()
                if row:
                    _cache = dict(row.value)
                    return _cache
                # Seed from JSON if a file exists
                seed = _load_json()
                session.add(db.KVConfig(key=CONFIG_KEY, value=seed))
                await session.commit()
                _cache = seed
                return seed
        except Exception as e:
            log.warning("config_db_read_failed", "Falling back to JSON config", context={"error": str(e)})
    _cache = _load_json()
    return _cache


async def save_config_async(cfg: dict) -> None:
    global _cache
    _cache = cfg
    if db.is_ready():
        try:
            async with db.SessionLocal() as session:
                row = (await session.execute(
                    select(db.KVConfig).where(db.KVConfig.key == CONFIG_KEY)
                )).scalar_one_or_none()
                if row:
                    row.value = cfg
                else:
                    session.add(db.KVConfig(key=CONFIG_KEY, value=cfg))
                await session.commit()
            return
        except Exception as e:
            log.warning("config_db_write_failed", "Falling back to JSON config", context={"error": str(e)})
    _write_json(cfg)


# ---------------------------------------------------------------------------
# Sync wrappers for legacy callers (used in non-async contexts only as a
# best-effort cache hit; the scheduled loops should call the async variants).
# ---------------------------------------------------------------------------

def load_config() -> dict:
    if _cache is not None:
        return dict(_cache)
    return _load_json()


def save_config(cfg: dict) -> None:
    """Sync save — writes JSON cache. Async callers should use save_config_async."""
    global _cache
    _cache = dict(cfg)
    _write_json(cfg)
