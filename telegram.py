"""Telegram: an owner-locked command bot, batched alerts, and health alerts (spec section 8).

* Owner lock: the first /start becomes the owner chat. Every other chat is ignored: no reply, no effect.
* Messages from the owner are commands for fixed code. There is no LLM here, and no text from the web, a coin name or anyone else
  is ever treated as an instruction (hard rule 7). Outgoing text is plain text (no markup parsing).
* There is NO withdraw command. LIVE can only be asked for by typing LIVE, and it is refused until every gate is met (rule 1).
* Every message in PAPER mode starts with "PAPER · ". FOMO links come only from links.py. The bot token is never logged.
* Everything is async: a slow Telegram never blocks the desk's event loop.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import shutil
import time

import httpx

import config
import gates as gates_mod
import links
import settings as st
import stats
from bots.common import subscribe_queue

log = logging.getLogger("desk.telegram")

API = "https://api.telegram.org"
BATCH_S = 20.0
HEALTH_REPEAT_S = 30 * 60
PAPER = "PAPER · "
CHUNK = 3800
OWNER_KEY, OFFSET_KEY = "telegram_owner_chat_id", "telegram_offset"
AGENT_NAMES = ["scanner", "analyst", "brain", "executor", "risk", "orion"]


class TelegramError(Exception):
    pass


def clean(text) -> str:
    """Plain text only: control characters out, length bounded. Coin names and symbols are data and pass through here."""
    s = "".join(c for c in str(text) if c == "\n" or (c.isprintable()))
    return s.strip()


def paper(text: str) -> str:
    return text if text.startswith(PAPER) else PAPER + text


def chunks(text: str, n: int = CHUNK) -> list[str]:
    if len(text) <= n:
        return [text]
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n and cur:
            out.append(cur)
            cur = ""
        cur += line + "\n"
    out.append(cur.rstrip("\n"))
    return [c for c in out if c]


# ---- the Bot API client -------------------------------------------------------------------------------------------------
class TelegramClient:
    def __init__(self, token: str, transport=None, base: str = API):
        self._token, self.base = token, base
        self._http = httpx.AsyncClient(transport=transport, timeout=40.0)

    async def call(self, method: str, params: dict | None = None, files: dict | None = None):
        url = f"{self.base}/bot{self._token}/{method}"
        try:
            if files:
                r = await self._http.post(url, data=params or {}, files=files)
            else:
                r = await self._http.post(url, json=params or {})
        except httpx.HTTPError as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None  # never the URL: it contains the token
        try:
            j = r.json()
        except ValueError:
            raise TelegramError(f"{method}: HTTP {r.status_code}") from None
        if not j.get("ok"):
            raise TelegramError(f"{method}: {str(j.get('description', 'error'))[:80]}")
        return j.get("result")

    async def send_text(self, chat_id: int, text: str, keyboard: list | None = None) -> None:
        parts = chunks(text)
        for i, part in enumerate(parts):
            params = {"chat_id": chat_id, "text": part, "disable_web_page_preview": True}
            if keyboard and i == len(parts) - 1:
                params["reply_markup"] = {"inline_keyboard": [keyboard]}
            await self.call("sendMessage", params)

    async def send_photo(self, chat_id: int, path, caption: str) -> None:
        with open(path, "rb") as f:
            await self.call("sendPhoto", {"chat_id": str(chat_id), "caption": caption[:1000]}, files={"photo": ("desk.png", f.read(), "image/png")})

    async def send_document(self, chat_id: int, path, caption: str = "") -> None:
        with open(path, "rb") as f:
            await self.call("sendDocument", {"chat_id": str(chat_id), "caption": caption[:1000]}, files={"document": (str(path).rsplit("/", 1)[-1], f.read(), "text/html")})

    async def answer_callback(self, cb_id: str) -> None:
        try:
            await self.call("answerCallbackQuery", {"callback_query_id": cb_id})
        except TelegramError:
            pass

    async def get_updates(self, offset: int, timeout: int = 25) -> list[dict]:
        return await self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}) or []

    async def aclose(self) -> None:
        await self._http.aclose()


# ---- formatting -----------------------------------------------------------------------------------------------------------
def status_line(cards: list[dict], feed_badge: str | None, running: int, total: int) -> str:
    """PAPER · prompt $150.00 (0 trades) · orion_fast $102.52 (-31.7%, 94 trades, 24% win) · feed PumpPortal live · 6 bots ok"""
    parts = []
    for c in cards:
        bal = "no data" if c["balance"] is None else f"${c['balance']:,.2f}"
        if c["trades"] == 0:
            detail = "0 trades"
        else:
            detail = f"{_pct(c['pnl_pct'])}, {c['trades']} trades, {c['win_rate']:.0f}% win"
        parts.append(f"{c['name']} {bal} ({detail})")
    parts.append(f"feed {feed_badge or 'no data'}")
    parts.append(f"{running} bots ok" if running == total else f"{running} of {total} bots ok")
    return PAPER + " · ".join(parts)


def _pct(v) -> str:
    if v is None:
        return "no data"
    r = round(v, 1)
    return ("−" if r < 0 else "+" if r > 0 else "") + f"{abs(r):.1f}%"


def money(v: float, sign: bool = False) -> str:
    r = round(v, 2)
    return ("−" if r < 0 else "+" if sign and r > 0 else "") + f"${abs(r):,.2f}"


def restart_text(bot: str, down_s: float) -> str:
    return f"Restarted {bot}, down {'<1' if down_s < 90 else round(down_s / 60)} min"


# ---- finding a coin by name, symbol or mint ----------------------------------------------------------------------------------
async def resolve(db, query: str, open_only: bool = False) -> list[dict]:
    q = clean(query)
    if not q:
        return []
    rows: list[dict] = []
    like = q.replace("%", "").replace("_", "")
    for sql, args in (
        ("SELECT mint, symbol, NULL name FROM positions WHERE closed_at IS NULL AND (lower(symbol)=lower(?) OR mint LIKE ?||'%')", (q, like)),
        ("SELECT mint, symbol, NULL name FROM strat_positions WHERE closed_at IS NULL AND (lower(symbol)=lower(?) OR mint LIKE ?||'%')", (q, like)),
    ):
        rows += await db.fetchall(sql, args)
    if not open_only:
        rows += await db.fetchall("SELECT mint, symbol, name FROM coins WHERE lower(symbol)=lower(?) OR lower(name)=lower(?) OR mint LIKE ?||'%' LIMIT 8", (q, q, like))
        rows += await db.fetchall("SELECT mint, symbol, name FROM new_tokens WHERE lower(symbol)=lower(?) OR lower(name)=lower(?) OR mint LIKE ?||'%' ORDER BY ts DESC LIMIT 8", (q, q, like))
        rows += await db.fetchall("SELECT mint, symbol, NULL name FROM positions WHERE lower(symbol)=lower(?) OR mint LIKE ?||'%' LIMIT 4", (q, like))
    seen, out = set(), []
    for r in rows:
        if r["mint"] not in seen:
            seen.add(r["mint"])
            out.append(r)
    return out[:6]


def pick_text(cands: list[dict], what: str) -> str:
    lines = [f"More than one coin matches. Send {what} again with the full mint of the one you mean:"]
    for c in cands:
        lines.append(f"{c.get('symbol') or '?'} · {c['mint'][:6]}...{c['mint'][-4:]}\n{links.coin_url(c['mint'])}")
    return "\n".join(lines)


HELP = """Commands (plain text or with a slash):
status: both strategies in one line
why X: why the desk did what it did with a coin (X = name, symbol or mint)
sell X [25|50|100]: sell some or all of an open paper position (asks you to confirm)
buy X: audit a coin the desk has scanned, show the size, then queue it as a dip order (asks you to confirm)
pause <bot> / resume <bot>: scanner, analyst, brain, executor, risk, orion (Risk cannot be paused while a position is open)
set <setting> to <value>: change one of your settings (validated and logged)
paper: confirm the mode
LIVE: ask for live trading (typed in capitals; refused until every gate is met)
live: a screenshot of the dashboard now
report: the night report
There is no withdraw command. Withdraw in the FOMO app yourself."""


# ---- the command bot ----------------------------------------------------------------------------------------------------------
class DeskBot:
    def __init__(self, client: TelegramClient, db, hub, clock=time.time, lan_url: str | None = None):
        self.client, self.db, self.hub, self.clock, self.lan_url = client, db, hub, clock, lan_url
        self.owner: int | None = None
        self.offset = 0
        self.pending: dict[int, dict] = {}
        self.ignored = 0

    async def load(self) -> None:
        v = await self.db.get_setting(OWNER_KEY)
        self.owner = int(v) if v else None
        o = await self.db.get_setting(OFFSET_KEY)
        self.offset = int(o) if o else 0

    async def _set(self, key: str, value) -> None:
        await self.db.execute("INSERT INTO settings(key, value, updated_at) VALUES (?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                              (key, str(value), self.clock()))

    async def say(self, chat: int, text: str, keyboard=None) -> None:
        await self.client.send_text(chat, paper(clean(text)), keyboard)

    async def handle_update(self, u: dict) -> None:
        cb = u.get("callback_query")
        if cb:
            chat = (cb.get("message") or {}).get("chat", {}).get("id")
            if self.owner is None or chat != self.owner:
                self.ignored += 1
                return
            await self.client.answer_callback(cb.get("id", ""))
            await self.dispatch(chat, cb.get("data") or "")
            return
        m = u.get("message")
        if not m or (m.get("chat") or {}).get("type") != "private":
            return
        chat, text = m["chat"]["id"], clean(m.get("text") or "")
        if not text:
            return  # a photo, a sticker, a join notice: nothing to answer
        if self.owner is None:
            if re.match(r"^/start(@\w+)?(\s|$)", text):
                self.owner = chat
                await self._set(OWNER_KEY, chat)
                log.info("owner chat linked")
                await self.say(chat, "Desk linked to this chat. I only answer you.\n" + HELP)
            else:
                self.ignored += 1
            return
        if chat != self.owner:
            self.ignored += 1  # not the owner: ignored, no reply
            return
        await self.dispatch(chat, text)

    # ---- dispatch -------------------------------------------------------------------------------------------------------------
    async def dispatch(self, chat: int, text: str) -> None:
        raw = text.strip()
        if raw == "LIVE":
            return await self.say(chat, await self.cmd_live_mode())
        body = raw[1:] if raw.startswith("/") else raw
        word, _, rest = body.partition(" ")
        word = word.split("@")[0].lower()
        rest = rest.strip()
        if word in ("yes", "y", "confirm", "ok"):
            return await self.confirm(chat, True)
        if word in ("no", "n", "cancel"):
            return await self.confirm(chat, False)
        handlers = {"start": self.cmd_help, "help": self.cmd_help, "status": self.cmd_status, "why": self.cmd_why, "sell": self.cmd_sell,
                    "buy": self.cmd_buy, "pause": self.cmd_pause, "resume": self.cmd_resume, "set": self.cmd_set, "paper": self.cmd_paper,
                    "report": self.cmd_report, "live": self.cmd_live}
        if word in ("withdraw", "deposit", "send", "transfer"):
            return await self.say(chat, "There is no withdraw command. Withdraw in the FOMO app yourself.")
        fn = handlers.get(word)
        if fn is None:
            return await self.say(chat, "I did not understand that. Send help for the list of commands.")
        try:
            reply = await fn(chat, rest)
        except Exception as e:  # noqa: BLE001 - a bad command must never take the bot down
            log.exception("command %s failed: %s", word, e)
            reply = f"That command failed: {type(e).__name__}. The desk is still running."
        if reply:
            kb = None
            if chat in self.pending and self.pending[chat].get("asked"):
                self.pending[chat]["asked"] = False
                kb = [{"text": "Yes, do it", "callback_data": "yes"}, {"text": "No", "callback_data": "no"}]
            await self.say(chat, reply, kb)

    async def confirm(self, chat: int, yes: bool) -> None:
        p = self.pending.pop(chat, None)
        if p is None or p["expires"] < self.clock():
            return await self.say(chat, "Nothing is waiting for a confirmation.")
        if not yes:
            return await self.say(chat, "Cancelled. Nothing was done.")
        await self.say(chat, await p["run"]())

    def ask(self, chat: int, prompt: str, run) -> str:
        self.pending[chat] = {"run": run, "expires": self.clock() + 90, "asked": True}
        return prompt + "\nReply yes within 90 seconds to confirm, or no."

    # ---- commands ---------------------------------------------------------------------------------------------------------------
    async def cmd_help(self, chat, rest):
        return HELP

    async def _cards(self):
        s = self.hub.objects.get("settings") or await st.Settings.load(self.db)
        marks = self.hub.objects["marks"]() if "marks" in self.hub.objects else {}
        return await stats.strategy_cards(self.db, float(s["paper_start_cash"]), self.clock(), marks)

    def _agents_running(self) -> tuple[int, int]:
        ages = self.hub.heartbeats.ages()
        return sum(1 for n in AGENT_NAMES if n in ages and ages[n] < 95), len(AGENT_NAMES)

    async def cmd_status(self, chat, rest):
        cards = await self._cards()
        feed = self.hub.status["feed"]()["badge"] if "feed" in self.hub.status else None
        run, tot = self._agents_running()
        lines = [status_line(cards, feed, run, tot)]
        for c in cards:
            for p in c["open"]:
                pnl = "no live price" if p["pnl_mark_usd"] is None else f"{money(p['pnl_mark_usd'], True)} ({_pct(p['pnl_mark_pct'])})"
                lines.append(f"open [{c['name']}] {p['symbol'] or p['mint'][:6]} {money(p['size_usd'])} {pnl}\n{links.coin_url(p['mint'])}")
        return "\n".join(lines)

    async def _one(self, rest: str, what: str, open_only=False):
        """(coin, None) or (None, reply text) when the name is unknown or ambiguous."""
        if not rest:
            return None, f"Which coin? Send: {what} <name, symbol or mint>"
        c = await resolve(self.db, rest, open_only)
        if not c:
            return None, f"I do not know {'an open position in ' if open_only else ''}'{clean(rest)[:40]}'. The desk only knows coins it has scanned."
        if len(c) > 1:
            return None, pick_text(c, what)
        return c[0], None

    async def cmd_why(self, chat, rest):
        coin, err = await self._one(rest, "why")
        if err:
            return err
        m = coin["mint"]
        rows = await self.db.fetchall("SELECT ts, bot, decision, reason, value FROM decisions WHERE mint=? ORDER BY id DESC LIMIT 8", (m,))
        lay = await self.db.fetchall('SELECT layer, name, value_text, "pass" p FROM audits WHERE mint=? ORDER BY id DESC LIMIT 16', (m,))
        score = await self.db.fetchone('SELECT score, "pass" p, reason FROM scores WHERE mint=? ORDER BY id DESC LIMIT 1', (m,))
        out = [f"{coin.get('symbol') or m[:6]}\n{links.coin_url(m)}"]
        if not rows and not lay:
            return out[0] + "\nThe desk has no decisions about this coin yet."
        out += [f"{time.strftime('%H:%M', time.localtime(r['ts']))} {r['bot']}: {r['decision']} · {r['reason']}" + (f" ({r['value']})" if r["value"] else "") for r in rows]
        latest = {}
        for r in lay:
            latest.setdefault(r["layer"], r)
        if latest:
            out.append("audit: " + ", ".join(f"{v['layer']} {v['name']} {'ok' if v['p'] == 1 else 'RED' if v['p'] == 0 else 'not checked'}" for _, v in sorted(latest.items())))
        if score:
            out.append(f"score {score['score']:.0f} ({'passed' if score['p'] == 1 else 'below the line'}) {score['reason'] or ''}")
        return "\n".join(out)

    async def cmd_sell(self, chat, rest):
        parts = rest.split()
        pct = 100
        if parts and parts[-1].isdigit() and len(parts) > 1:
            pct = int(parts.pop())
        if pct not in (25, 50, 100):
            return "Sell 25, 50 or 100 percent: sell X 50"
        coin, err = await self._one(" ".join(parts), "sell", open_only=True)
        if err:
            return err
        mint = coin["mint"]

        async def run():
            self.hub.bus.publish("manual_sell", {"mint": mint, "pct": pct})
            return f"Sell queued: {pct}% of {coin.get('symbol') or mint[:6]} (paper, at the last real price, minus fees)."
        return self.ask(chat, f"Sell {pct}% of {coin.get('symbol') or mint[:6]} (paper)?\n{links.coin_url(mint)}", run)

    async def cmd_buy(self, chat, rest):
        coin, err = await self._one(rest, "buy")
        if err:
            return err
        mint = coin["mint"]
        o = self.hub.objects
        if not all(k in o for k in ("analyst", "brain_size", "book")):
            return "Buying from Telegram needs the desk's bots to be running."
        row = await self.db.fetchone("SELECT * FROM coins WHERE mint=?", (mint,))
        if row is None:
            return f"The desk never scanned {coin.get('symbol') or mint[:6]}, so it will not buy it. It only buys coins that went through the Scanner."
        scan = await self.db.fetchone("SELECT liquidity_usd FROM scans WHERE mint=? AND liquidity_usd IS NOT NULL ORDER BY id DESC LIMIT 1", (mint,))
        c = {"mint": mint, "symbol": row["symbol"], "name": row["name"], "pool": row["pool"], "creator": row["creator"], "curve_min": row["curve_min"],
             "creator_balance_at_migration": row["creator_balance_at_migration"], "migrated_at": row["migrated_at"],
             "liq_at_scan": scan["liquidity_usd"] if scan else None}
        await self.say(chat, f"Auditing {row['symbol'] or mint[:6]}: 8 layers and the score. This takes a few seconds.")
        res = await o["analyst"].audit(c)
        layers = ", ".join(f"{l.n} {'ok' if l.ok else 'RED ' + l.value}" for l in res["layers"])
        if not res["passed"]:
            return f"Not bought: {res['reason']}.\naudit: {layers}\nI never override a failed audit."
        sz = o["brain_size"](res["payload"])
        if sz.skip:
            return f"Audit passed (score {res['score']}) but the Brain skips it: {sz.reason}."
        payload = {**res["payload"], "size_usd": sz.size_usd, "pct_of_cash": sz.pct_of_cash, "cap_hit": sz.cap_hit}

        async def run():
            self.hub.bus.publish("order_request", payload)
            return f"Queued: {money(sz.size_usd)} of {row['symbol'] or mint[:6]} as a dip order (it waits for a dip, up to 30 minutes)."
        return self.ask(chat, f"{row['symbol'] or mint[:6]}: 8/8 green, score {res['score']}.\nSize {money(sz.size_usd)} ({sz.pct_of_cash:.1f}% of cash, cap {sz.cap_hit}). Paper.\n{links.coin_url(mint)}", run)

    async def cmd_pause(self, chat, rest, on=True):
        name = rest.strip().lower()
        if name not in AGENT_NAMES:
            return "Which bot? " + ", ".join(AGENT_NAMES)
        if on and name == "risk":
            can = self.hub.objects["risk_pausable"]() if "risk_pausable" in self.hub.objects else True
            if not can:
                return "Risk cannot be paused while a position is open."
        (self.hub.paused.add if on else self.hub.paused.discard)(name)
        return f"{name} {'paused' if on else 'resumed'}."

    async def cmd_resume(self, chat, rest):
        return await self.cmd_pause(chat, rest, on=False)

    async def cmd_set(self, chat, rest):
        m = re.match(r"^(\w+)\s+to\s+(.+)$", rest.strip(), re.I)
        if not m:
            return "Use: set <setting> to <value>, for example: set kill_liq_usd to 25000"
        s = self.hub.objects.get("settings") or await st.Settings.load(self.db)
        try:
            old, new = await s.set(self.db, m.group(1), m.group(2).strip(), st.OWNER)  # the owner chat is the owner
        except st.SettingError as e:
            return f"Not changed: {e}"
        return f"{m.group(1)}: {old} → {new} (logged)."

    async def cmd_paper(self, chat, rest):
        return "Mode is PAPER. It is the only mode this build has: nothing here moves real money."

    async def cmd_live_mode(self) -> str:
        g = await gates_mod.live_gates(self.db)
        lines = ["LIVE is locked. It needs every gate, and then your typed LIVE:"] + [f"{'yes' if x['met'] else 'no '} {x['gate']}" for x in g]
        if all(x["met"] for x in g):
            lines.append(gates_mod.LIVE_NOT_BUILT + ".")
        return "\n".join(lines)

    async def cmd_report(self, chat, rest):
        import report

        gen = self.hub.objects.get("report_fn") or (lambda: report.build_night_report(self.db, self.hub))
        res = await gen()
        link = f"{self.lan_url}/#report={res['date']}" if self.lan_url else f"(open the dashboard and use /#report={res['date']})"
        await self.client.send_document(chat, res["path"], paper(f"Night report {res['date']}"))
        return f"Night report {res['date']}: {res['summary']}\n{link}"

    async def cmd_live(self, chat, rest):
        ls = self.hub.objects.get("live_shots")
        status = await self.cmd_status(chat, "")
        if ls is None:
            return status + "\n(Screenshots are not running.)"
        ls.request_now("telegram /live")
        return status + "\nScreenshot on its way."


# ---- batched alerts + health alerts ---------------------------------------------------------------------------------------------
class ErrorCounter(logging.Handler):
    """Counts ERROR log lines so a burst of exceptions can raise one alert."""

    def __init__(self, window_s: float = 60.0):
        super().__init__(logging.ERROR)
        self.window_s, self.times = window_s, collections.deque()

    def emit(self, record):
        self.times.append(time.time())

    def recent(self, now: float | None = None) -> int:
        now = time.time() if now is None else now
        while self.times and self.times[0] < now - self.window_s:
            self.times.popleft()
        return len(self.times)


class HealthWatcher:
    """One alert per issue per 30 minutes: bot stale > 2 min, feed down > 2 min, CDP down, disk < 5 GB, loop lag p95 > 500 ms, exception burst."""

    def __init__(self, hub, errors: ErrorCounter | None = None, cdp_check=None, disk_free=None, clock=time.time):
        self.hub, self.errors, self.clock = hub, errors, clock
        self.cdp_check = cdp_check  # () -> bool (reachable) or None to skip
        self.disk_free = disk_free or (lambda: shutil.disk_usage(config.ROOT).free)
        self.last_sent: dict[str, float] = {}
        self.feed_down_since: float | None = None

    def _due(self, key: str, now: float) -> bool:
        if now - self.last_sent.get(key, -1e12) >= HEALTH_REPEAT_S:
            self.last_sent[key] = now
            return True
        return False

    def check(self) -> list[str]:
        now, out = self.clock(), []
        for name, age in sorted(self.hub.heartbeats.ages().items()):
            if age > 120 and self._due(f"stale:{name}", now):
                out.append(f"Bot {name} has been silent for {round(age / 60)} min.")
        feed = self.hub.status["feed"]()["badge"] if "feed" in self.hub.status else None
        if feed == "feed down":
            self.feed_down_since = self.feed_down_since or now
            if now - self.feed_down_since > 120 and self._due("feed", now):
                out.append(f"The market feed has been down for {round((now - self.feed_down_since) / 60)} min.")
        else:
            self.feed_down_since = None
        if self.cdp_check is not None and not self.cdp_check() and self._due("cdp", now):
            out.append("The desk Chrome (DevTools) is not answering. It will be relaunched; log in again if FOMO asks.")
        free = self.disk_free()
        if free < 5 * 1024 ** 3 and self._due("disk", now):
            out.append(f"Disk space is low: {free / 1024 ** 3:.1f} GB free.")
        p95 = self.hub.lag.snapshot()["p95"]
        if p95 is not None and p95 > 500 and self._due("lag", now):
            out.append(f"The desk is lagging: event-loop p95 {p95:.0f} ms.")
        if self.errors is not None and self.errors.recent(now) >= 10 and self._due("errors", now):
            out.append(f"A burst of errors: {self.errors.recent(now)} in the last minute. See logs/desk.log.")
        return out


class Notifier:
    """Collects what the bots push, and sends one message per 20 s window, tagged by strategy, with the coin link."""

    def __init__(self, client: TelegramClient, bot: DeskBot, hub, health: HealthWatcher | None = None, clock=time.time, batch_s: float = BATCH_S):
        self.client, self.bot, self.hub, self.health, self.clock, self.batch_s = client, bot, hub, health, clock, batch_s
        self.q_push = subscribe_queue(hub, "push")
        self.q_restart = subscribe_queue(hub, "restart")
        self.q_out = subscribe_queue(hub, "fomo_logged_out")
        self.q_in = subscribe_queue(hub, "fomo_resumed")
        self.buf: list[str] = []
        self.first_at: float | None = None
        self.sent = 0

    def _add(self, line: str) -> None:
        if self.first_at is None:
            self.first_at = self.clock()
        self.buf.append(line)

    async def collect(self) -> None:
        for d in drain(self.q_push):
            text = clean(d.get("text", ""))
            if d.get("strategy") and d["strategy"] not in text:
                text += f" [{d['strategy']}]"
            self._add(text + (f"\n{d['link']}" if d.get("link") else ""))
        for d in drain(self.q_restart):
            self._add(restart_text(d.get("bot", "?"), d.get("down_s", 1.0)))
        for _ in drain(self.q_out):
            self._add("FOMO logged out. Log in on the laptop; no new buys until you do.")
        for _ in drain(self.q_in):
            self._add("FOMO session is back.")
        if self.health is not None:  # the Chrome probe blocks for up to a second or two: never on the event loop
            for line in await asyncio.to_thread(self.health.check):
                self._add(line)

    async def flush(self, force: bool = False) -> bool:
        if not self.buf or self.bot.owner is None:
            return False
        if not force and self.clock() - (self.first_at or 0) < self.batch_s:
            return False
        lines, first = self.buf, self.first_at
        self.buf, self.first_at = [], None
        try:
            await self.client.send_text(self.bot.owner, paper("\n".join(lines)))
            self.sent += 1
        except TelegramError as e:
            log.warning("alert not sent, will retry: %s", e)
            self.buf, self.first_at = lines + self.buf, first  # nothing is lost: the next window carries it
            return False
        return True

    async def run(self) -> None:
        from bus import beating_sleep

        while True:
            self.hub.heartbeats.beat("notifier")
            await self.collect()
            await self.flush()
            await beating_sleep(self.hub, "notifier", 1.0, step=1.0)


def drain(q: asyncio.Queue) -> list:
    out = []
    while True:
        try:
            out.append(q.get_nowait())
        except asyncio.QueueEmpty:
            return out


# ---- the long-poll loop -------------------------------------------------------------------------------------------------------------
async def poll_loop(bot: DeskBot, hub, timeout: int = 25, sleep=asyncio.sleep) -> None:
    """Long polling with backoff 1,2,4 ... 60 s. Updates are acknowledged (offset saved) before they are handled, so a crash never replays a command."""
    await bot.load()
    delay = 1.0
    while True:
        hub.heartbeats.beat("telegram")
        try:
            updates = await bot.client.get_updates(bot.offset, timeout)
            delay = 1.0
        except TelegramError as e:
            log.warning("telegram poll failed: %s (retry in %.0fs)", e, delay)
            for _ in range(int(delay)):
                hub.heartbeats.beat("telegram")
                await sleep(1)
            delay = min(60.0, delay * 2)
            continue
        for u in updates:
            bot.offset = u["update_id"] + 1
            await bot._set(OFFSET_KEY, bot.offset)
            try:
                await bot.handle_update(u)
            except TelegramError as e:
                log.warning("reply failed: %s", e)
            except Exception as e:  # noqa: BLE001
                log.exception("update failed: %s", e)
        if not updates:
            await sleep(0)


# ---- the 08:00 report ------------------------------------------------------------------------------------------------------------------
async def report_once(bot: DeskBot, hub, clock=time.time) -> str | None:
    """If it is past report_time and today's report has not gone out, build it and send it. Returns the date sent, or None."""
    import datetime as dt

    import report

    s = hub.objects.get("settings") or await st.Settings.load(bot.db)
    h, m = (int(x) for x in s["report_time"].split(":"))
    now = dt.datetime.fromtimestamp(clock())
    today = now.date().isoformat()
    if now.time() < dt.time(h, m) or await bot.db.get_setting("last_report_date") == today:
        return None
    res = await report.build_night_report(bot.db, hub)
    await bot._set("last_report_date", today)
    if bot.owner is None:
        return None  # built, but nobody has said /start yet
    link = f"{bot.lan_url}/#report={res['date']}" if bot.lan_url else ""
    await bot.client.send_document(bot.owner, res["path"], paper(f"Night report {res['date']}"))
    await bot.say(bot.owner, f"Night report {res['date']}: {res['summary']}.\n{link}".strip())
    closed = (await bot.db.fetchone("SELECT COUNT(*) n FROM nights WHERE ended_at IS NOT NULL"))["n"]
    if closed == 1 and await bot.db.get_setting("usage_sent") != "1":  # after the first PAPER night: what it cost
        await bot._set("usage_sent", 1)
        await bot.say(bot.owner, "What your first night cost: " + report.usage_text(hub))
    if closed and closed % 7 == 0 and await bot.db.get_setting("last_weekly_nights") != str(closed):
        await bot._set("last_weekly_nights", closed)
        wk = await report.weekly_review(bot.db, s)
        path = report.write_weekly(wk, res["date"])
        await bot.client.send_document(bot.owner, path, paper(f"Weekly review {res['date']}: {len(wk['suggestions'])} suggestion(s), none applied"))
    return res["date"]


async def report_job(bot: DeskBot, hub, clock=time.time, sleep=asyncio.sleep, every: float = 30.0) -> None:
    while True:
        hub.heartbeats.beat("report_job")
        try:
            await report_once(bot, hub, clock)
        except TelegramError as e:
            log.warning("report not sent: %s", e)
        except Exception as e:  # noqa: BLE001
            log.exception("report job failed: %s", e)
        await sleep(every)
