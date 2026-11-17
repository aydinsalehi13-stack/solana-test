import base64
import json
import os

import httpx
import pytest

import sources as sc
from intel import jupiter as jp
from conftest import ROOT

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58decode(s):
    n = 0
    for ch in s:
        n = n * 58 + ALPHABET.index(ch)
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return b"\0" * (len(s) - len(s.lstrip("1"))) + body


def test_b58_roundtrip_and_known_vectors():
    assert sc.b58encode(b"\x00\x01") == "12" and sc.b58encode(bytes(32)) == "1" * 32
    for _ in range(20):
        raw = os.urandom(32)
        assert b58decode(sc.b58encode(raw)) == raw
    assert sc.b58encode(b58decode("So11111111111111111111111111111111111111112")) == "So11111111111111111111111111111111111111112"


class RpcServer:
    def __init__(self, table):
        self.table, self.requests = table, []

    def __call__(self, req):
        body = json.loads(req.content)
        self.requests.append(body)
        out = self.table[body["method"]](body["params"])
        if isinstance(out, httpx.Response):
            return out
        if isinstance(out, Exception):
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32010, "message": str(out)}})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": out})


def rpc(table):
    srv = RpcServer(table)
    return sc.Rpc(sc.Clients(transport=httpx.MockTransport(srv)), url="https://rpc.test"), srv


MINT_ACC = {"value": {"data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": "F1", "supply": "1000000000000000", "decimals": 6}}}}}


async def test_mint_info_parses_and_caches_10_minutes():
    r, srv = rpc({"getAccountInfo": lambda p: MINT_ACC})
    info = await r.mint_info("M")
    assert info == {"mint_authority": None, "freeze_authority": "F1", "supply": 1e9, "decimals": 6}
    await r.mint_info("M")
    assert len(srv.requests) == 1
    assert srv.requests[0]["params"][1] == {"encoding": "jsonParsed"}


async def test_mint_info_missing_account_is_an_error_not_a_guess():
    r, _ = rpc({"getAccountInfo": lambda p: {"value": None}})
    with pytest.raises(sc.SourceError):
        await r.mint_info("M")


async def test_rpc_errors_and_rate_limits():
    r, _ = rpc({"getAccountInfo": lambda p: Exception("method not supported")})
    with pytest.raises(sc.SourceError, match="-32010"):
        await r.mint_info("M")
    r, _ = rpc({"getAccountInfo": lambda p: httpx.Response(429)})
    with pytest.raises(sc.RateLimited):
        await r.mint_info("M")
    r, _ = rpc({"getAccountInfo": lambda p: httpx.Response(503)})
    with pytest.raises(sc.SourceError):
        await r.mint_info("M")


async def test_top10_excludes_pool_owner_and_uses_supply():
    amounts = [400, 100, 90, 80, 70, 60, 50, 40, 30, 20, 10, 5]
    r, _ = rpc({
        "getAccountInfo": lambda p: MINT_ACC | {"value": {"data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None, "supply": "1000000000", "decimals": 6}}}}},
        "getTokenLargestAccounts": lambda p: {"value": [{"address": f"a{i}", "uiAmount": a} for i, a in enumerate(amounts)]},
        "getMultipleAccounts": lambda p: {"value": [{"data": {"parsed": {"info": {"owner": "POOLOWNER" if a == "a0" else f"o-{a}"}}}} for a in p[0]]},
    })
    assert await r.top10_pct("M", {"POOLOWNER"}) == pytest.approx(55.0)  # (100+90+...+10) / supply 1000
    assert await r.top10_pct("M", set()) == pytest.approx(94.0)  # 400+100+90+80+70+60+50+40+30+20 = 940 of 1000, nothing excluded


async def test_token_balance_sums_accounts_and_empty_is_zero():
    acct = lambda v: {"account": {"data": {"parsed": {"info": {"tokenAmount": {"uiAmount": v}}}}}}  # noqa: E731
    r, _ = rpc({"getTokenAccountsByOwner": lambda p: {"value": [acct(10.5), acct(4.5)]}})
    assert await r.token_balance("DEV", "M") == 15.0
    r, srv = rpc({"getTokenAccountsByOwner": lambda p: {"value": []}})
    assert await r.token_balance("DEV", "M") == 0.0
    assert srv.requests[0]["params"][1] == {"mint": "M"}
    r, _ = rpc({"getTokenAccountsByOwner": lambda p: {}})
    assert await r.token_balance("DEV", "M") is None


def acct_bytes(owner: bytes, amount_raw: int, delegate: bytes | None):
    return owner + amount_raw.to_bytes(8, "little") + (1 if delegate else 0).to_bytes(4, "little") + (delegate or bytes(32))


async def test_delegations_parse_the_real_token_account_layout_and_count_credits():
    d1, d2 = bytes([7]) * 32, bytes([9]) * 32
    items = [{"pubkey": "x", "account": {"data": [base64.b64encode(acct_bytes(bytes([1]) * 32, 5_000_000, d1)).decode(), "base64"]}},
             {"pubkey": "y", "account": {"data": [base64.b64encode(acct_bytes(bytes([2]) * 32, 9_000_000, d1)).decode(), "base64"]}},
             {"pubkey": "z", "account": {"data": [base64.b64encode(acct_bytes(bytes([3]) * 32, 1_000_000, None)).decode(), "base64"]}}]
    t22 = [{"pubkey": "w", "account": {"data": [base64.b64encode(acct_bytes(bytes([4]) * 32, 2_000_000, d2)).decode(), "base64"]}}]
    r, srv = rpc({"getProgramAccounts": lambda p: items if p[0] == sc.TOKEN_PROGRAM else t22})
    out = await r.delegations("M", 6)
    assert out["accounts"] == 4 and out["by_delegate"] == {sc.b58encode(d1): 2, sc.b58encode(d2): 1}
    assert out["top_holders"][0] == (sc.b58encode(bytes([2]) * 32), 9.0) and len(out["top_holders"]) == 4
    token_call = next(b for b in srv.requests if b["params"][0] == sc.TOKEN_PROGRAM)
    cfg = token_call["params"][1]
    assert cfg["dataSlice"] == {"offset": 32, "length": 76} and {"dataSize": 165} in cfg["filters"]
    assert {"memcmp": {"offset": 0, "bytes": "M"}} in cfg["filters"]
    t22_call = next(b for b in srv.requests if b["params"][0] == sc.TOKEN_2022_PROGRAM)
    assert {"dataSize": 165} not in t22_call["params"][1]["filters"]
    assert r.credits_today() == 20  # getProgramAccounts costs 10 credits each


async def test_delegations_refused_raises_for_the_analyst_to_report():
    r, _ = rpc({"getProgramAccounts": lambda p: Exception("excluded from account secondary indexes")})
    with pytest.raises(sc.SourceError):
        await r.delegations("M", 6)


async def test_signature_count_and_helius_url_is_never_the_label():
    r, srv = rpc({"getSignaturesForAddress": lambda p: [{"signature": "a"}] * 7})
    assert await r.signature_count("O", 20) == 7 and srv.requests[0]["params"][1] == {"limit": 20}
    h = sc.Rpc(sc.Clients(), helius_key="KEY123")
    assert "KEY123" in h._url and h.label == "helius" and "KEY123" not in h.label


# ---- RugCheck / Coinbase ------------------------------------------------------------------------------------------------------------
def test_insiders_pct_cases():
    assert sc.insiders_pct({"insiderNetworks": [{"tokenAmount": 1e8}, {"tokenAmount": 2e8}], "token": {"supply": 1e9}}) == pytest.approx(30.0)
    assert sc.insiders_pct({"insiderNetworks": None}) == 0.0
    assert sc.insiders_pct({"token": {"supply": 1}}) is None  # no key: cannot say
    assert sc.insiders_pct({"insiderNetworks": [{"tokenAmount": 1}]}) is None  # no supply
    assert sc.insiders_pct(None) is None


def test_biggest_bundle_connected_components():
    g = {"nodes": [{}] * 5, "links": [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}, {"source": "d", "target": "e"}]}
    assert sc.biggest_bundle(g) == 3
    assert sc.biggest_bundle({"edges": [{"source": 1, "target": 2}]}) == 2
    assert sc.biggest_bundle({"nodes": [1, 2], "links": []}) == 1
    assert sc.biggest_bundle({"nodes": [1, 2]}) is None and sc.biggest_bundle(None) is None


def test_lp_for_pool():
    rep = {"markets": [{"pubkey": "P1", "lp": {"lpLockedPct": 100}}, {"pubkey": "P2", "lp": {}}]}
    assert sc.lp_for_pool(rep, "P1") == {"locked_pct": 100.0} and sc.lp_for_pool(rep, "P2") == {"locked_pct": None}
    assert sc.lp_for_pool(rep, "P3") is None and sc.lp_for_pool(rep, None) is None and sc.lp_for_pool(None, "P1") is None


async def test_rugcheck_caches_report_2_minutes():
    n = []

    def h(req):
        n.append(str(req.url))
        return httpx.Response(200, json={"markets": []})

    rc = sc.RugCheck(sc.Clients(transport=httpx.MockTransport(h)))
    await rc.report("M")
    await rc.report("M")
    assert len(n) == 1 and n[0].endswith("/v1/tokens/M/report")


async def test_coinbase_spot():
    cb = sc.Coinbase(sc.Clients(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {"amount": "152.34"}}))))
    assert await cb.spot() == 152.34
    bad = sc.Coinbase(sc.Clients(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": {}}))))
    with pytest.raises(sc.SourceError):
        await bad.spot()


# ---- Jupiter ------------------------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("key", ["taker", "userPublicKey", "wallet", "payer", "destinationTokenAccount", "feeAccount", "TAKER"])
def test_jupiter_refuses_wallet_fields(key):
    with pytest.raises(ValueError, match="never carry wallet"):
        jp.clean_params({"inputMint": "a", key: "x"})


def test_jupiter_code_has_no_wallet_or_swap_execution():
    t = (ROOT / "intel" / "jupiter.py").read_text()
    for bad in ("/swap/v1/swap", "swap-instructions", "Keypair", "sign"):
        assert bad not in t.replace("signer", "").replace("clean_params", "")


def jup_clients(handler):
    return sc.Clients(transport=httpx.MockTransport(handler))


async def test_round_trip_math_and_requests_carry_no_wallet():
    seen = []

    def h(req):
        seen.append(req.url)
        q = dict(req.url.params)
        if q["inputMint"] == jp.SOL_MINT:
            return httpx.Response(200, json={"outAmount": "5000000", "priceImpactPct": "0.001"})
        return httpx.Response(200, json={"outAmount": "194000000", "priceImpactPct": "0.02"})

    async def nosleep(d):
        pass

    j = jp.Jupiter(jup_clients(h), sleep=nosleep)
    r = await j.round_trip("MINT", usd=20.0, sol_usd=100.0)  # 0.2 SOL = 200,000,000 lamports
    assert r["rt_loss_pct"] == pytest.approx(3.0) and r["sell_impact_pct"] == pytest.approx(2.0) and r["tokens_raw"] == 5_000_000
    assert dict(seen[0].params)["amount"] == "200000000" and dict(seen[1].params)["amount"] == "5000000"
    for u in seen:
        assert not ({k.lower() for k in dict(u.params)} & jp.FORBIDDEN_PARAMS)


async def test_round_trip_without_a_route_raises():
    async def nosleep(d):
        pass

    j = jp.Jupiter(jup_clients(lambda r: httpx.Response(200, json={"outAmount": "0"})), sleep=nosleep)
    with pytest.raises(sc.SourceError, match="no buy route"):
        await j.round_trip("M", 20, 100)


async def test_quote_falls_back_to_v2_order_without_taker():
    paths = []

    def h(req):
        paths.append(req.url.path)
        return httpx.Response(404) if req.url.path == "/swap/v1/quote" else httpx.Response(200, json={"outAmount": "1"})

    async def nosleep(d):
        pass

    out = await jp.Jupiter(jup_clients(h), sleep=nosleep).quote("a", "b", 5)
    assert paths == ["/swap/v1/quote", "/swap/v2/order"] and out["outAmount"] == "1"


async def test_429_backoff_is_min_max_gap_x1_5_1_to_6():
    gaps = []

    class T:
        t = 0.0

    def clock():
        return T.t

    async def sleep(d):
        T.t += d

    j = jp.Jupiter(jup_clients(lambda r: httpx.Response(429)), sleep=sleep, clock=clock)
    orig = j._get

    with pytest.raises(sc.RateLimited):
        await j._get("/swap/v1/quote", {"inputMint": "a"})
    assert j.gap == pytest.approx(3.375)  # 1.0 -> 1.5 -> 2.25 -> 3.375 over three 429s
    j.gap = 5.0
    with pytest.raises(sc.RateLimited):
        await j._get("/swap/v1/quote", {"inputMint": "a"})
    assert j.gap == 6.0  # capped
