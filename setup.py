"""One-time / repair setup, same on Windows, macOS and Linux.

  python setup.py                 install dependencies, create .env (locked to your user), make folders
  python setup.py --with-browser  also run `playwright install chromium`

The desk's own APP_TOKEN is generated locally and written to .env without printing it. Open .env in an
editor to read it. Other secrets (Telegram, Helius, ...) you type into .env yourself; never paste them in chat.
"""
from __future__ import annotations

import importlib
import re
import secrets
import subprocess
import sys

import config
import osutil

REQUIRED_MODULES = ("fastapi", "uvicorn", "httpx", "websockets", "playwright", "pydantic")


def missing_modules() -> list[str]:
    out = []
    for m in REQUIRED_MODULES:
        try:
            importlib.import_module(m)
        except ImportError:
            out.append(m)
    return out


def install_requirements() -> int:
    return subprocess.call([sys.executable, "-m", "pip", "install", "-r", str(config.ROOT / "requirements.txt")])


def ensure_env_file() -> str:
    """Create .env from .env.example, give the desk an APP_TOKEN if blank, lock the file down."""
    env, example = config.ENV_FILE, config.ROOT / ".env.example"
    created = not env.exists()
    if created:
        env.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    text = env.read_text(encoding="utf-8")
    if not config.parse_env(text).get("APP_TOKEN"):
        token = secrets.token_urlsafe(32)
        if re.search(r"^APP_TOKEN=.*$", text, re.M):
            text = re.sub(r"^APP_TOKEN=.*$", lambda _: f"APP_TOKEN={token}", text, count=1, flags=re.M)
        else:
            text += f"\nAPP_TOKEN={token}\n"
        env.write_text(text, encoding="utf-8")
        note = "APP_TOKEN generated"
    else:
        note = "APP_TOKEN already set"
    locked = osutil.lock_down(env)
    return f".env {'created' if created else 'found'}, {note}, {'locked to your user' if locked else 'COULD NOT lock permissions'}"


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    config.ensure_dirs()
    miss = missing_modules()
    if miss:
        osutil.say(f"installing dependencies (missing: {', '.join(miss)})")
        if install_requirements() != 0:
            osutil.say("pip install failed")
            return 1
    if "--with-browser" in argv:
        subprocess.call([sys.executable, "-m", "playwright", "install", "chromium"])
    osutil.say(ensure_env_file())
    osutil.say("setup done. Open .env in an editor to see APP_TOKEN or add other keys.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
