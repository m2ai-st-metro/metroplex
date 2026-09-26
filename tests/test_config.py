from cos.config import Config


def _clear(monkeypatch):
    for key in ("METROPLEX_APPROVER_IDS", "TELETRAAN_WORK_APPROVER_IDS"):
        monkeypatch.delenv(key, raising=False)


def test_approvers_fall_back_to_work_service_list(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("TELETRAAN_WORK_APPROVER_IDS", "111, 222")
    assert Config.from_env().approver_ids == ("111", "222")


def test_explicit_metroplex_approvers_win(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("METROPLEX_APPROVER_IDS", "333")
    monkeypatch.setenv("TELETRAAN_WORK_APPROVER_IDS", "111")
    assert Config.from_env().approver_ids == ("333",)


def test_no_approvers_stays_empty(monkeypatch):
    _clear(monkeypatch)
    assert Config.from_env().approver_ids == ()
