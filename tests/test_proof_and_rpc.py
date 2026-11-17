import httpx
import pytest

import prove_session as ps
import rpc_probe as rp
from db import Database

MAP = {"user.id": {"source": "userHandle", "path": "id", "confirmed": True},
       "user.address": {"source": "userHandle", "path": "wallet.address", "confirmed": True}}
ADDR = "So11111111111111111111111111111111111111112"


class FakeBrowser:
    def __init__(self):
        self.opened = []

    async def ensure_ready(self):
        return {"avatar": True, "cash": "$150.00", "wide": 1440}

    async def ensure_page(self):
        class P:
            async def screenshot(self, **kw):
                if kw.get("path"):
                    open(kw["path"], "wb").write(b"png")

        return P()

    async def open_coin(self, mint):
        self.opened.append(mint)


class FakeApi:
    def __init__(self, raw):
        self.raw = raw

    async def user_by_handle(self, h):
        return self.raw


@pytest.fixture
async def db(tmp_path):
    d = Database(tmp_path / "p.db")
    await d.start()
    yield d
    await d.stop()


async def test_proof_saves_ids_and_masks_address(db, tmp_path, capsys):
    res = await ps.prove(FakeBrowser(), FakeApi({"id": "u1", "wallet": {"address": ADDR}}), db, out_dir=tmp_path, fmap=MAP, mint="M")
    assert res["ok"] and res["address"] == "So11...1112" and ADDR not in str(res)
    assert (await db.get_setting("fomo_user_id")) == "u1" and (await db.get_setting("fomo_address")) == ADDR
    assert len(res["shots"]) == 2


async def test_proof_tells_owner_what_is_missing_instead_of_guessing(db, tmp_path):
    res = await ps.prove(FakeBrowser(), FakeApi({"id": "u1"}), db, out_dir=tmp_path, fmap=MAP)
    assert not res["ok"] and "fields.py --suggest" in res["needs"]
    assert await db.get_setting("fomo_user_id") is None


def test_endpoints_helius_first_and_key_not_in_label():
    eps = rp.endpoints({"HELIUS_API_KEY": "KEYVALUE123"})
    assert eps[0][0] == "helius" and "KEYVALUE123" in eps[0][1] and "KEYVALUE123" not in eps[0][0]
    assert [e[0] for e in rp.endpoints({})] == ["solana-mainnet-beta", "publicnode"]


def test_gpa_payload_shape():
    p = rp.gpa_payload(rp.TOKEN_PROGRAM, "MINT", True)
    cfg = p["params"][1]
    assert p["method"] == "getProgramAccounts" and cfg["dataSlice"] == {"offset": 0, "length": 0}
    assert {"dataSize": 165} in cfg["filters"] and {"memcmp": {"offset": 0, "bytes": "MINT"}} in cfg["filters"]
    assert {"dataSize": 165} not in rp.gpa_payload(rp.TOKEN_2022_PROGRAM, "MINT", False)["params"][1]["filters"]


async def test_probe_allowed_counts_accounts():
    def handler(req):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": [{"pubkey": "a"}, {"pubkey": "b"}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await rp.probe("MINT", [("x", "https://rpc.test")], c)
    assert res["allowed"] and res["results"][0]["token_accounts"] == 2


async def test_probe_refused_gives_the_exact_spec_message_and_hides_keys():
    def handler(req):
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"code": -32010, "message": "excluded from account secondary indexes"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await rp.probe("MINT", [("helius", "https://rpc.test/?api-key=SECRETKEY")], c)
    assert not res["allowed"]
    assert res["verdict"] == "Analyst layer 8 turns every coin red until rpc_url allows getProgramAccounts"
    assert "SECRETKEY" not in str(res)


async def test_probe_falls_through_to_next_endpoint():
    def handler(req):
        if req.url.host == "bad.test":
            return httpx.Response(403)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        res = await rp.probe("MINT", [("bad", "https://bad.test"), ("good", "https://good.test")], c)
    assert res["allowed"] and [r["ok"] for r in res["results"]] == [False, True]
