"""Raw-JSON key mapping. Bots read FOMO data ONLY through this module (spec 3.2).

fields_map.json says, per field the bots need, which source it comes from and the exact key path.
Rules: a missing key is None, never a guess. A path of null means "not mapped yet". `confirmed` is true
only after the path was checked against a real raw sample in logs/raw/ (python fields.py shows the table).

    python fields.py             table: field the bots need -> exact key, checked against logs/raw/
    python fields.py --suggest   for unmapped/missing fields, list leaf keys in the sample that look related
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import config

MAP_FILE = config.ROOT / "fields_map.json"
STABLES_AND_MAJORS = {"USDC", "USDT", "SOL", "WETH"}
_TOKEN = re.compile(r"[^.\[\]]+|\[\d+\]")


def load_map(path: Path | None = None) -> dict:
    return json.loads((path or MAP_FILE).read_text(encoding="utf-8"))["fields"]


def parse_path(path: str) -> list:
    out: list = []
    for t in _TOKEN.findall(path):
        out.append(int(t[1:-1]) if t.startswith("[") else t)
    return out


def dig(raw, path: str | None):
    """Follow a path like 'data.items[0].amount'. Any miss returns None."""
    if path is None:
        return None
    cur = raw
    for seg in parse_path(path):
        try:
            cur = cur[seg]
        except (KeyError, IndexError, TypeError):
            return None
    return cur


def get(field: str, raw, fmap: dict | None = None):
    """Value of a mapped field from one raw response, or None."""
    entry = (fmap or load_map()).get(field)
    return None if entry is None else dig(raw, entry.get("path"))


def classify_side(side_value, in_symbol, out_symbol) -> str | None:
    """Swap side: the side key if present, otherwise into USDC/USDT/SOL/WETH = sell, out of them = buy."""
    if isinstance(side_value, str) and side_value.strip().lower() in ("buy", "sell"):
        return side_value.strip().lower()
    i = (in_symbol or "").upper()
    o = (out_symbol or "").upper()
    if o in STABLES_AND_MAJORS and i not in STABLES_AND_MAJORS:
        return "sell"
    if i in STABLES_AND_MAJORS and o not in STABLES_AND_MAJORS:
        return "buy"
    return None


# ---- report -------------------------------------------------------------------------------------
def _sample(source: str, raw_dir: Path):
    p = raw_dir / f"{source}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def report(raw_dir: Path | None = None, fmap: dict | None = None) -> list[dict]:
    raw_dir = raw_dir or config.LOG_DIR / "raw"
    rows = []
    for name, e in (fmap or load_map()).items():
        sample = _sample(e["source"], raw_dir)
        if e.get("path") is None:
            status = "UNMAPPED (needs a raw sample)"
        elif sample is None:
            status = "no sample yet"
        elif dig(sample, e["path"]) is None:
            status = "MISSING in sample"
        else:
            status = "found" + ("" if e.get("confirmed") else " (set confirmed:true)")
        rows.append({"field": name, "source": e["source"], "path": e.get("path"),
                     "confirmed": bool(e.get("confirmed")), "status": status})
    return rows


def leaf_paths(raw, prefix: str = "", limit: int = 400) -> list[tuple[str, str]]:
    """(path, short value) for every leaf, strings cut to 12 chars so no address is shown in full."""
    out: list[tuple[str, str]] = []

    def walk(node, p):
        if len(out) >= limit:
            return
        if isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{p}.{k}" if p else k)
        elif isinstance(node, list):
            for i, v in enumerate(node[:2]):
                walk(v, f"{p}[{i}]")
        else:
            s = str(node)
            out.append((p, s if len(s) <= 12 else s[:12] + "..."))

    walk(raw, prefix)
    return out


def suggest(field: str, sample) -> list[tuple[str, str]]:
    words = [w for w in re.split(r"[._]", field.lower()) if len(w) > 2]
    return [(p, v) for p, v in leaf_paths(sample) if any(w in p.lower() for w in words)][:8]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    rows = report()
    w = max(len(r["field"]) for r in rows)
    print(f"{'field':<{w}}  {'source':<14} {'exact key':<34} status")
    for r in rows:
        print(f"{r['field']:<{w}}  {r['source']:<14} {str(r['path']):<34} {r['status']}")
    if "--suggest" in argv:
        fmap = load_map()
        for r in rows:
            if r["status"].startswith(("UNMAPPED", "MISSING")):
                s = _sample(r["source"], config.LOG_DIR / "raw")
                if s is not None:
                    print(f"\n{r['field']} (candidates in {r['source']} sample):")
                    for p, v in suggest(r["field"], s) or [("(none)", "")]:
                        print(f"   {p} = {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
