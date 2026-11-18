"""No fake stuff, ever: the product holds no demo data and no made-up numbers. Fake rows exist only inside tests (tests/seed.py),
in throwaway databases, to check that the screens draw correctly. Nothing here can reach the real desk."""
import re

from conftest import ROOT

PRODUCT = [p for p in list(ROOT.glob("*.py")) + list(ROOT.glob("bots/*.py")) + list(ROOT.glob("intel/*.py")) + list(ROOT.glob("tools/*.py"))]
WEB = list((ROOT / "web").rglob("*.js")) + list((ROOT / "web").rglob("*.html")) + list((ROOT / "web").rglob("*.css"))


def test_no_product_file_knows_about_demo_data():
    assert len(PRODUCT) > 30 and len(WEB) >= 9
    bad = [f.name for f in PRODUCT + WEB if re.search(r"seed_demo|DemoMint|demo[ _-]?data|import seed|from seed|from tests|fake_telegram|sample data|placeholder (price|balance)", f.read_text(errors="ignore"), re.I)]
    assert bad == []


def test_there_is_no_demo_mode_switch_anywhere():
    bad = [f.name for f in PRODUCT + WEB if re.search(r"DEMO_MODE|DESK_DEMO|demoMode|[?&]demo=|is_demo|mock_data|MOCK_", f.read_text(errors="ignore"))]
    assert bad == []


def test_the_page_never_makes_up_a_number():
    """Randomness is allowed only where it is decoration: confetti pieces and the sideways drift of a dropped dot. Never for data."""
    hits = []
    for f in WEB:
        for i, line in enumerate(f.read_text().splitlines(), 1):
            if "Math.random" in line:
                hits.append((f.name, "confetti" if "innerWidth" in line else "drop drift" if "kind: 'drop'" in line else f"line {i}"))
    assert sorted(hits) == [("flow.js", "drop drift"), ("main.js", "confetti")]
    js = "\n".join(f.read_text() for f in WEB if f.suffix == ".js")
    assert "no data" in js and not re.search(r"\|\|\s*(0|150|300)\s*\)?\s*;?\s*//\s*default (balance|price)", js)


def test_no_demo_pictures_are_shipped_in_the_repo():
    import subprocess

    tracked = subprocess.run(["git", "ls-files", "proof"], cwd=ROOT, capture_output=True, text=True).stdout.split()
    assert [t for t in tracked if t.startswith("proof/phase6/") and t.endswith(".png")] == []
