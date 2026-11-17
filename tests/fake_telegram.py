"""A fake Telegram Bot API (httpx MockTransport). Records what the desk sends; replays updates the test pushes."""
import json
import re

import httpx

from telegram import TelegramClient

TOKEN = "123456:TESTTOKEN-ABCDEFGHIJKLMNOP"


class FakeTelegram:
    def __init__(self):
        self.updates, self.sent, self.calls = [], [], []
        self.fail_methods: set[str] = set()
        self._uid = 100

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert TOKEN in str(request.url), "the token is part of the Bot API URL"
        method = request.url.path.rsplit("/", 1)[-1]
        self.calls.append(method)
        if method in self.fail_methods:
            return httpx.Response(200, json={"ok": False, "description": "Too Many Requests: retry later"})
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            body = json.loads(request.content or b"{}")
        else:  # multipart upload: pull out the plain fields
            raw = request.content.decode("utf-8", "replace")
            body = {m.group(1): m.group(2) for m in re.finditer(r'name="(\w+)"\r\n\r\n(.*?)\r\n--', raw, re.S)}
            body["_bytes"] = len(request.content)
        if method == "getUpdates":
            off = body.get("offset", 0)
            return httpx.Response(200, json={"ok": True, "result": [u for u in self.updates if u["update_id"] >= off]})
        if method in ("sendMessage", "sendPhoto", "sendDocument"):
            self.sent.append({"method": method, **body})
            return httpx.Response(200, json={"ok": True, "result": {"message_id": len(self.sent)}})
        return httpx.Response(200, json={"ok": True, "result": True})

    def client(self) -> TelegramClient:
        return TelegramClient(TOKEN, transport=httpx.MockTransport(self))

    # ---- helpers for tests ----
    def say(self, chat_id: int, text: str, chat_type: str = "private") -> dict:
        self._uid += 1
        u = {"update_id": self._uid, "message": {"message_id": self._uid, "chat": {"id": chat_id, "type": chat_type}, "text": text}}
        self.updates.append(u)
        return u

    def tap(self, chat_id: int, data: str) -> dict:
        self._uid += 1
        u = {"update_id": self._uid, "callback_query": {"id": f"cb{self._uid}", "data": data, "message": {"chat": {"id": chat_id, "type": "private"}}}}
        self.updates.append(u)
        return u

    def texts(self, method="sendMessage"):
        return [s.get("text") or s.get("caption") or "" for s in self.sent if s["method"] == method]
