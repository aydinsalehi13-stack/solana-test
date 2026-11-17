"""One log file per bot, rotated daily. Secret values are scrubbed from every record (hard rule 5)."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler

import config

_FMT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
_bot_handlers: dict[str, logging.Handler] = {}


class RedactFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secrets = config.secret_values()
        if not secrets:
            return True
        msg = record.getMessage()
        for s in secrets:
            msg = msg.replace(s, "***")
        record.msg, record.args = msg, None
        return True


def _file_handler(name: str) -> logging.Handler:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    h = TimedRotatingFileHandler(
        config.LOG_DIR / f"{name}.log", when="midnight", backupCount=14, encoding="utf-8", delay=True
    )
    h.setFormatter(_FMT)
    h.addFilter(RedactFilter())
    return h


def setup_logging(level: int = logging.INFO, console: bool = True) -> logging.Logger:
    root = logging.getLogger("desk")
    root.setLevel(level)
    if root.handlers:
        return root
    root.addHandler(_file_handler("desk"))
    if console and sys.stderr is not None:
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(_FMT)
        sh.addFilter(RedactFilter())
        root.addHandler(sh)
    return root


def bot_logger(name: str) -> logging.Logger:
    """logs/<name>.log for that bot, and it also flows into logs/desk.log."""
    lg = logging.getLogger(f"desk.{name}")
    if name not in _bot_handlers:
        h = _file_handler(name)
        lg.addHandler(h)
        _bot_handlers[name] = h
    return lg
