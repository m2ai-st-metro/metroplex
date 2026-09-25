"""@m2ai_metroplex_bot transport: long-poll updates, send with inline buttons.

Plain text only (no parse_mode), so task titles with < or > can never break a
message. Only updates from an approver's user id in the configured chat are
passed on; everything else is dropped and logged, never answered.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger(__name__)

Transport = Callable[[str, dict[str, Any]], Any]


def http_transport(token: str) -> Transport:
    base = f"https://api.telegram.org/bot{token}"

    def call(method: str, payload: dict[str, Any]) -> Any:
        timeout = payload.get("timeout", 0) + 10
        req = urllib.request.Request(f"{base}/{method}", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
        if not body.get("ok"):
            raise RuntimeError(f"TELEGRAM_{method}_{body.get('error_code')}")
        return body.get("result")

    return call


@dataclass
class Inbound:
    kind: str  # "text" | "callback"
    text: str
    from_id: str
    chat_id: str
    message_id: str
    callback_id: str | None = None


class Bot:
    def __init__(self, transport: Transport, chat_id: str, approver_ids: tuple[str, ...]):
        self.call = transport
        self.chat_id = str(chat_id)
        self.approvers = {str(a) for a in approver_ids}

    def send(self, text: str, buttons: list[list[dict[str, str]]] | None = None) -> str | None:
        payload: dict[str, Any] = {"chat_id": self.chat_id, "text": text[:4000], "disable_web_page_preview": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            result = self.call("sendMessage", payload)
            return str(result.get("message_id")) if isinstance(result, dict) else None
        except Exception as e:  # noqa: BLE001 - a failed notice must not stop the loop
            log.warning("telegram send failed: %s", e)
            return None

    def answer(self, callback_id: str, text: str) -> None:
        try:
            self.call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:200]})
        except Exception as e:  # noqa: BLE001
            log.warning("telegram callback answer failed: %s", e)

    def poll(self, offset: int, timeout: int = 25) -> tuple[list[Inbound], int]:
        updates = self.call("getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}) or []
        inbound: list[Inbound] = []
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            if "callback_query" in u:
                cq = u["callback_query"]
                msg = cq.get("message") or {}
                item = Inbound("callback", cq.get("data") or "", str(cq["from"]["id"]), str(msg.get("chat", {}).get("id")), str(msg.get("message_id")), cq["id"])
            elif "message" in u and "text" in u["message"]:
                m = u["message"]
                item = Inbound("text", m["text"], str(m["from"]["id"]), str(m["chat"]["id"]), str(m["message_id"]))
            else:
                continue
            if item.from_id not in self.approvers or item.chat_id != self.chat_id:
                log.warning("dropped update from user %s in chat %s", item.from_id, item.chat_id)
                continue
            inbound.append(item)
        return inbound, offset
