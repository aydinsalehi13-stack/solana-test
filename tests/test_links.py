import re

import links
from conftest import ROOT

SKIP = {"links.py", "test_links.py", "test_paths.py", "SPEC.md"}


def test_coin_url_exact():
    m = "4CxjZyAbCdEfGhIjKlMnOpQrStUvWxYz8spump"
    assert links.coin_url(m) == f"https://fomo.family/coin?address={m}&chainId=1399811149&source=share_link"
    assert links.coin_url(m, "base").endswith("chainId=8453&source=share_link")


def test_unknown_chain_returns_signup(caplog):
    assert links.coin_url("x", "doge") == links.SIGNUP_URL == "https://fomo.family"
    assert "unknown chain" in caplog.text


def test_profile_url_strips_at():
    assert links.profile_url("@fluffcore3") == "https://fomo.family/profile/fluffcore3?source=share_link"
    assert links.profile_url("fluffcore3") == links.profile_url("@fluffcore3")


def test_no_fomo_site_urls_outside_links_module(code_files):
    pat = re.compile(r"https?://(www\.)?fomo\.family")
    bad = [str(f.relative_to(ROOT)) for f in code_files if f.name not in SKIP and pat.search(f.read_text(errors="ignore"))]
    assert not bad, f"FOMO URLs must come from links.py: {bad}"


def test_no_referral_anywhere(code_files):
    bad = []
    for f in code_files:
        if f.name in SKIP:
            continue
        t = f.read_text(errors="ignore")
        if "savipww" in t or "fomo.family/r/" in t:
            bad.append(f.name)
    assert not bad
