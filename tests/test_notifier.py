"""
Tests for Metroplex Notifier - Telegram + LogNotifier
"""
import json
import pytest
from unittest.mock import patch, MagicMock
import urllib.error

from notifier import LogNotifier, TelegramNotifier, create_notifier, FilteredNotifier, Notifier


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


# --- Phase 13d: FilteredNotifier ---


class RecordingNotifier:
    """Test double that records all notifications."""

    def __init__(self):
        self.messages: list[tuple[str, str]] = []

    def notify(self, message: str, level: str = "info") -> bool:
        self.messages.append((message, level))
        return True


@pytest.fixture
def recorder():
    return RecordingNotifier()


class TestFilteredNotifierAllMode:
    """In 'all' mode, everything passes through."""

    def test_info_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "all")
        fn.notify("hello", "info")
        assert len(recorder.messages) == 1

    def test_warning_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "all")
        fn.notify("alert", "warning")
        assert len(recorder.messages) == 1

    def test_error_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "all")
        fn.notify("boom", "error")
        assert len(recorder.messages) == 1


class TestFilteredNotifierAnomalyMode:
    """In 'anomaly' mode, only warning/error pass through."""

    def test_info_suppressed(self, recorder):
        fn = FilteredNotifier(recorder, "anomaly")
        result = fn.notify("all good", "info")
        assert result is True
        assert len(recorder.messages) == 0

    def test_warning_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "anomaly")
        fn.notify("heads up", "warning")
        assert len(recorder.messages) == 1
        assert recorder.messages[0] == ("heads up", "warning")

    def test_error_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "anomaly")
        fn.notify("crash", "error")
        assert len(recorder.messages) == 1

    def test_info_with_error_keyword_still_suppressed(self, recorder):
        """In anomaly mode, info messages are suppressed even if they mention 'error'."""
        fn = FilteredNotifier(recorder, "anomaly")
        fn.notify("Metroplex: 0 triaged, 1 built, 1 errors", "info")
        assert len(recorder.messages) == 0


class TestFilteredNotifierSummaryMode:
    """In 'summary' mode, warning/error pass through, plus info with 'error' keyword."""

    def test_info_suppressed(self, recorder):
        fn = FilteredNotifier(recorder, "summary")
        fn.notify("all good", "info")
        assert len(recorder.messages) == 0

    def test_warning_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "summary")
        fn.notify("heads up", "warning")
        assert len(recorder.messages) == 1

    def test_error_forwarded(self, recorder):
        fn = FilteredNotifier(recorder, "summary")
        fn.notify("crash", "error")
        assert len(recorder.messages) == 1

    def test_info_with_error_keyword_forwarded(self, recorder):
        """Summary mode lets cycle summaries through when they mention errors."""
        fn = FilteredNotifier(recorder, "summary")
        fn.notify("Metroplex: 0 triaged, 1 built, 1 errors", "info")
        assert len(recorder.messages) == 1

    def test_info_without_error_keyword_suppressed(self, recorder):
        fn = FilteredNotifier(recorder, "summary")
        fn.notify("Metroplex: 2 triaged, 1 built, 0 patched", "info")
        assert len(recorder.messages) == 0


class TestFilteredNotifierWrapsLogNotifier:
    """Integration: FilteredNotifier wraps a real LogNotifier."""

    def test_anomaly_wrapping_log_notifier(self):
        log = LogNotifier()
        fn = FilteredNotifier(log, "anomaly")
        result = fn.notify("info message", "info")
        assert result is True
        result = fn.notify("error message", "error")
        assert result is True
