"""Paths, environment and secrets. Every path is relative to the project root."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# Overridable only for tests / alternative layouts; defaults live inside the project.
DATA_DIR = Path(os.environ.get("DESK_DATA_DIR") or ROOT / "data")
LOG_DIR = Path(os.environ.get("DESK_LOG_DIR") or ROOT / "logs")
PROFILE_DIR = Path(os.environ.get("DESK_PROFILE_DIR") or ROOT / "chrome-profile")
ENV_FILE = ROOT / ".env"
DB_PATH = DATA_DIR / "desk.db"
PID_FILE = DATA_DIR / "desk.pid"

CDP_HOST = "127.0.0.1"  # never anything else (hard rule 6)
DEFAULT_CDP_PORT = 9222
DEFAULT_API_PORT = 8787

# Hard rule 5: the only places a secret may live. Names only, never values.
SECRET_KEYS = (
    "APP_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "HELIUS_API_KEY",
    "PUMPPORTAL_API_KEY",
    "JUPITER_API_KEY",
    "GMGN_API_KEY",
    "XAI_API_KEY",
    "VAPID_PUBLIC_KEY",
    "VAPID_PRIVATE_KEY",
    "SOCIAL_WEBHOOK_URL",
)

# The desk always starts in PAPER. Nothing in this module can change it (hard rule 1).
DEFAULT_MODE = "PAPER"


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]
        out[key.strip()] = val
    return out


def load_env(path: Path | None = None, environ: dict | None = None) -> dict[str, str]:
    """Load .env into the environment. Real environment variables win."""
    env = os.environ if environ is None else environ
    p = Path(path) if path else ENV_FILE
    loaded: dict[str, str] = {}
    if p.exists():
        for k, v in parse_env(p.read_text(encoding="utf-8")).items():
            loaded[k] = v
            env.setdefault(k, v)
    return loaded


def secret_values(environ: dict | None = None) -> list[str]:
    env = os.environ if environ is None else environ
    return [v for k in SECRET_KEYS if (v := env.get(k, "")) and len(v) >= 6]


def desk_host() -> str:
    return os.environ.get("DESK_HOST") or "127.0.0.1"


def desk_port() -> int:
    return int(os.environ.get("DESK_PORT") or DEFAULT_API_PORT)


def cdp_port() -> int:
    return int(os.environ.get("DESK_CDP_PORT") or DEFAULT_CDP_PORT)


def ensure_dirs() -> None:
    for d in (DATA_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)
