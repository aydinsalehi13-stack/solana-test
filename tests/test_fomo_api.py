import asyncio
import json

import pytest

import fomo_api as fa
from bus import Hub
from fomo_api import FomoApi


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.t

    async def sleep(self, d):
        self.sleeps.append(d)
        self.t += d
        await asyncio.sleep(0)


def make(responses=None, **kw):
    clk = Clock()
    calls = []
    script = list(responses or [])

    async def fetcher(url, method, body):
        calls.append((url, method, body))
        status, text = script.pop(0) if script else (200, "{}")
        return status, text

    api = FomoApi(fetcher, sleep=clk.sleep, clock=clk, **kw)
    return api, calls, clk


async def test_gap_between_calls_is_1_2s():
    api, calls, clk = make()
    for _ in range(3):
        await api.leaderboard("24h")
    assert len(calls) == 3
    assert clk.sleeps == [pytest.approx(1.2), pytest.approx(1.2)]
    await api.close()


async def test_priority_verify_risk_executor_analyst_scanner():
    gate = asyncio.Event()
    order = []

    async def fetcher(url, method, body):
        if not order:
            await gate.wait()
        order.append(url.rsplit("/", 1)[-1])
        return 200, "{}"

    clk = Clock()
    api = FomoApi(fetcher, sleep=clk.sleep, clock=clk)
    first = asyncio.create_task(api.request("/a_first", bot="scanner"))
    await asyncio.sleep(0.01)
    tasks = [asyncio.create_task(api.request(f"/{b}", bot=b)) for b in ("scanner", "analyst", "executor", "risk", "verify")]
    await asyncio.sleep(0.01)
    gate.set()
    await asyncio.gather(first, *tasks)
    assert order == ["a_first", "verify", "risk", "executor", "analyst", "scanner"]
    await api.close()


async def test_429_doubles_gap_then_eases_back():
    api, calls, clk = make([(429, ""), (200, "{}"), (200, "{}"), (200, "{}")])
    await api.leaderboard("24h")
    assert len(calls) == 2 and api.gap == pytest.approx(max(1.2, 2.4 * 0.9))
    for _ in range(10):
        await api.leaderboard("7d")
    assert api.gap == pytest.approx(1.2)
    await api.close()


async def test_gap_never_exceeds_20s_and_gives_up():
    api, calls, _ = make([(429, "")] * 3)
    api.gap = 15
    with pytest.raises(fa.RateLimited):
        await api.leaderboard("24h")
    assert api.gap == 20.0
    await api.close()


async def test_logged_out_after_two_401_then_resumes_on_one_200():
    hub = Hub()
    api, _, _ = make([(401, ""), (401, ""), (200, "{}")], hub=hub)
    with pytest.raises(fa.LoggedOut):
        await api.leaderboard("24h")
    assert api.session_ok()  # one 401 is not enough
    with pytest.raises(fa.LoggedOut):
        await api.leaderboard("24h")
    assert not api.session_ok() and hub.bus.recent(topic="fomo_logged_out")
    await api.leaderboard("24h")
    assert api.session_ok() and hub.bus.recent(topic="fomo_resumed")
    await api.close()


async def test_scanner_reads_skip_when_queue_over_40_but_verify_never_does():
    gate = asyncio.Event()

    async def fetcher(url, method, body):
        await gate.wait()
        return 200, "{}"

    clk = Clock()
    api = FomoApi(fetcher, sleep=clk.sleep, clock=clk)
    pending = [asyncio.create_task(api.request(f"/x{i}", bot="analyst")) for i in range(45)]
    await asyncio.sleep(0.02)
    assert api.scanner_should_skip()
    with pytest.raises(fa.QueueBusy):
        await api.filter_tokens(["MINT"], droppable=True)
    v = asyncio.create_task(api.request("/verify", bot="verify"))
    gate.set()
    await asyncio.gather(*pending, v)
    await api.close()


async def test_filter_tokens_body_and_limit():
    api, calls, _ = make()
    await api.filter_tokens(["AAA", "BBB"])
    url, method, body = calls[0]
    assert url == "https://prod-api.fomo.family/proxy/filterTokens" and method == "POST"
    assert body == ["AAA:1399811149", "BBB:1399811149"]
    with pytest.raises(ValueError):
        await api.filter_tokens([f"m{i}" for i in range(21)])
    with pytest.raises(ValueError):
        await api.filter_tokens([])
    await api.close()


async def test_bodies_are_never_guessed():
    api, calls, _ = make()
    with pytest.raises(fa.NeedsCapturedBody):
        await api.token_details(None)
    with pytest.raises(fa.NeedsCapturedBody):
        await api.token_warnings(None)
    assert calls == []
    await api.token_warnings({"copied": "from the tab"})
    assert calls[0][2] == {"copied": "from the tab"}
    await api.close()


async def test_one_raw_sample_per_source(tmp_path):
    api, _, _ = make([(200, '{"a":1}'), (200, '{"a":2}')], raw_dir=tmp_path)
    await api.leaderboard("24h")
    await api.leaderboard("24h")
    assert json.loads((tmp_path / "leaderboard.json").read_text()) == {"a": 1}
    await api.close()


async def test_large_json_parsed_off_the_loop():
    big = json.dumps({"rows": list(range(8000))})
    assert len(big) > 20000
    api, _, _ = make([(200, big)])
    assert len((await api.leaderboard("24h"))["rows"]) == 8000
    await api.close()


async def test_page_fetcher_uses_credentials_include_and_no_cookie_read():
    seen = {}

    class Page:
        async def evaluate(self, js, arg):
            seen["js"], seen["arg"] = js, arg
            return {"status": 200, "text": "{}"}

    async def get_page():
        return Page()

    st, tx = await fa.page_fetcher(get_page)("https://prod-api.fomo.family/x", "GET", None)
    assert (st, tx) == (200, "{}")
    assert "credentials: 'include'" in seen["js"] and "cookie" not in seen["js"].lower()


def test_request_recorder_stores_method_path_body_only(tmp_path):
    handlers = {}

    class Page:
        def on(self, ev, fn):
            handlers[ev] = fn

    class Req:
        url, method, post_data = "https://prod-api.fomo.family/proxy/tokenDetails?x=1", "POST", '["m:1"]'
        headers = {"cookie": "SECRET"}

    fa.attach_request_recorder(Page(), tmp_path)
    handlers["request"](Req())
    handlers["request"](type("O", (), {"url": "https://other.example/x", "method": "GET", "post_data": None})())
    files = list(tmp_path.glob("*.json"))
    assert [f.name for f in files] == ["proxy_tokenDetails.json"]
    assert "SECRET" not in files[0].read_text() and json.loads(files[0].read_text())["body"] == '["m:1"]'
