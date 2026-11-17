import json

import fields


def test_dig_paths_and_misses_are_none():
    raw = {"a": {"b": [{"c": 5}]}}
    assert fields.dig(raw, "a.b[0].c") == 5
    assert fields.dig(raw, "a.x") is None and fields.dig(raw, "a.b[3]") is None and fields.dig(raw, None) is None


def test_get_unmapped_field_is_none_never_guessed():
    assert fields.get("swap.side", {"side": "buy"}) is None  # path is null: no guessing
    assert fields.get("no.such.field", {}) is None


def test_mapped_spec_keys_work():
    assert fields.get("warnings.disable_selling", {"disableSelling": True}) is True
    assert fields.get("details.top10_pct", {"top10HoldersPercent": 12.5}) == 12.5


def test_classify_side_prefers_key_then_direction():
    assert fields.classify_side("BUY", None, None) == "buy"
    assert fields.classify_side(None, "PAWS", "USDC") == "sell"
    assert fields.classify_side(None, "SOL", "PAWS") == "buy"
    assert fields.classify_side(None, "PAWS", "BONK") is None  # unknown: no guess


def test_map_file_is_well_formed():
    m = fields.load_map()
    assert len(m) >= 20
    for name, e in m.items():
        assert {"source", "path", "confirmed"} <= set(e), name
        assert e["confirmed"] is False  # nothing is confirmed until a real sample says so


def test_report_statuses(tmp_path):
    fmap = {"a": {"source": "s1", "path": "x", "confirmed": True}, "b": {"source": "s1", "path": "y", "confirmed": False},
            "c": {"source": "s2", "path": "z", "confirmed": False}, "d": {"source": "s1", "path": None, "confirmed": False}}
    (tmp_path / "s1.json").write_text(json.dumps({"x": 1}))
    st = {r["field"]: r["status"] for r in fields.report(tmp_path, fmap)}
    assert st["a"] == "found" and st["b"] == "MISSING in sample" and st["c"] == "no sample yet" and st["d"].startswith("UNMAPPED")


def test_suggest_truncates_long_values():
    sample = {"user": {"id": "abcdefghijklmnopqrstuvwxyz", "walletAddress": "So11111111111111111111111111111111111111112"}}
    got = dict(fields.suggest("user.address", sample))
    assert got["user.walletAddress"].endswith("...") and len(got["user.walletAddress"]) <= 15
