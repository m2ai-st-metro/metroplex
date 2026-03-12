"""
Metroplex Notifier
Pluggable notification system. Supports Telegram (via Bot API) and log-only fallback.
No external dependencies -- uses urllib.request for HTTP.
"""
import json
import logging
import urllib.request
import urllib.error
from typing import Protocol

logger = logging.getLogger(__name__)


class Notifier(Protocol):
    """Notification protocol. Implementations send messages to external systems."""

    def notify(self, message: str, level: str = "info") -> bool:
        """Send a notification. Returns True if delivered."""
        ...


class LogNotifier:
    """Fallback notifier that just logs. Used when no external service is configured."""

    def notify(self, message: str, level: str = "info") -> bool:
        log_fn = getattr(logger, level, logger.info)
        log_fn(f"[Notification] {message}")
        print(f"[notify:{level}] {message}")
        return True


class TelegramNotifier:
    """Send notifications via Telegram Bot API. Zero external dependencies."""

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self.api_base = f"https://api.telegram.org/bot{bot_token}"

    def notify(self, message: str, level: str = "info") -> bool:
        """Send a Telegram message. Returns True on success."""
        prefix = ""
        if level == "error":
            prefix = "!! "
        elif level == "warning":
            prefix = "! "

        payload = json.dumps({
            "chat_id": self.chat_id,
            "text": f"{prefix}{message}",
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }).encode("utf-8")

        req = urllib.request.Request(
            f"{self.api_base}/sendMessage",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status == 200
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            logger.warning(f"Telegram send failed: {e}")
            # Don't crash the pipeline over a notification failure
            return False


class FilteredNotifier:
    """Wraps a Notifier and filters messages based on notify_mode.

    Modes:
        all     — forward everything (no filtering)
        anomaly — only forward warning + error level messages
        summary — same as anomaly, plus cycle summaries that contain errors
    """

    def __init__(self, inner: Notifier, mode: str = "all"):
        self._inner = inner
        self.mode = mode

    def notify(self, message: str, level: str = "info") -> bool:
        if self.mode == "all":
            return self._inner.notify(message, level)

        # anomaly and summary: forward warning/error
        if level in ("warning", "error"):
            return self._inner.notify(message, level)

        # summary mode: also forward cycle summaries that mention errors
        if self.mode == "summary" and "error" in message.lower():
            return self._inner.notify(message, level)

        # Suppressed — still log locally
        logger.debug("[filtered:%s] %s", level, message)
        return True


def create_notifier(bot_token: str | None, chat_id: str | None) -> Notifier:
    """Factory: returns TelegramNotifier if configured, else LogNotifier."""
    if bot_token and chat_id:
        logger.info("Telegram notifications enabled")
        return TelegramNotifier(bot_token, chat_id)
    else:
        logger.info("No Telegram config -- using log-only notifications")
        return LogNotifier()
