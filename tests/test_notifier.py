"""
Tests for Metroplex Notifier - Telegram + LogNotifier
"""
import json
import pytest
from unittest.mock import patch, MagicMock
import urllib.error

from notifier import LogNotifier, TelegramNotifier, create_notifier, Notifier


class TestLogNotifier:
    """Test LogNotifier fallback."""

    def test_notify_returns_true(self):
        notifier = LogNotifier()
        assert notifier.notify("test message") is True

    def test_notify_with_level(self):
        notifier = LogNotifier()
        assert notifier.notify("error message", level="error") is True
        assert notifier.notify("warning message", level="warning") is True
        assert notifier.notify("info message", level="info") is True

    def test_notify_prints_to_stdout(self, capsys):
        notifier = LogNotifier()
        notifier.notify("hello world", level="info")
        captured = capsys.readouterr()
        assert "[notify:info] hello world" in captured.out

    def test_notify_prints_error_level(self, capsys):
        notifier = LogNotifier()
        notifier.notify("something broke", level="error")
        captured = capsys.readouterr()
        assert "[notify:error] something broke" in captured.out


class TestTelegramNotifier:
    """Test TelegramNotifier with mocked HTTP."""

    def test_init_stores_credentials(self):
        notifier = TelegramNotifier("bot-token-123", "chat-456")
        assert notifier.bot_token == "bot-token-123"
        assert notifier.chat_id == "chat-456"
        assert "bot-token-123" in notifier.api_base

    def test_notify_sends_http_request(self):
        notifier = TelegramNotifier("bot-token", "chat-id")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            result = notifier.notify("test message")

            assert result is True
            assert mock_urlopen.called

            # Verify the request was constructed correctly
            call_args = mock_urlopen.call_args
            req = call_args[0][0]
            assert req.full_url.endswith("/sendMessage")
            assert req.method == "POST"

            # Verify payload
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["chat_id"] == "chat-id"
            assert payload["text"] == "test message"
            assert payload["parse_mode"] == "HTML"

    def test_notify_error_prefix(self):
        notifier = TelegramNotifier("bot-token", "chat-id")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            notifier.notify("bad thing happened", level="error")

            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["text"].startswith("!! ")

    def test_notify_warning_prefix(self):
        notifier = TelegramNotifier("bot-token", "chat-id")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.__enter__ = MagicMock(return_value=mock_resp)
            mock_resp.__exit__ = MagicMock(return_value=False)
            mock_urlopen.return_value = mock_resp

            notifier.notify("heads up", level="warning")

            req = mock_urlopen.call_args[0][0]
            payload = json.loads(req.data.decode("utf-8"))
            assert payload["text"].startswith("! ")

    def test_notify_handles_url_error(self):
        notifier = TelegramNotifier("bot-token", "chat-id")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("network error")
            result = notifier.notify("test message")

            # Should return False but not raise
            assert result is False

    def test_notify_handles_http_error(self):
        notifier = TelegramNotifier("bot-token", "chat-id")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://api.telegram.org/test",
                code=403,
                msg="Forbidden",
                hdrs={},
                fp=None,
            )
            result = notifier.notify("test message")
            assert result is False

    def test_notify_handles_timeout(self):
        notifier = TelegramNotifier("bot-token", "chat-id")

        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = TimeoutError("connection timed out")
            result = notifier.notify("test message")
            assert result is False


class TestCreateNotifier:
    """Test create_notifier factory function."""

    def test_returns_telegram_when_configured(self):
        notifier = create_notifier("my-bot-token", "my-chat-id")
        assert isinstance(notifier, TelegramNotifier)

    def test_returns_log_when_no_token(self):
        notifier = create_notifier(None, "my-chat-id")
        assert isinstance(notifier, LogNotifier)

    def test_returns_log_when_no_chat_id(self):
        notifier = create_notifier("my-bot-token", None)
        assert isinstance(notifier, LogNotifier)

    def test_returns_log_when_empty_strings(self):
        notifier = create_notifier("", "")
        assert isinstance(notifier, LogNotifier)

    def test_returns_log_when_both_none(self):
        notifier = create_notifier(None, None)
        assert isinstance(notifier, LogNotifier)
