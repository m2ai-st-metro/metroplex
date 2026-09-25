"""Fixes for findings from the first live end-to-end run (2026-09-25)."""

from __future__ import annotations

from cos.intake import CLASSIFY_SYSTEM, DECOMPOSE_SYSTEM, infer_reserved
from tests.test_intake_daemon import ScriptedReasoner, button, daemon, text, yes_button


def test_e1_button_tap_is_acknowledged_before_the_slow_work_starts(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    order = []
    d.bot.answer = lambda cid, t: order.append(("answer", t))
    real_approve = d.intake.approve
    d.intake.approve = lambda *a, **k: (order.append(("approve", None)), real_approve(*a, **k))[1]
    d.handle(button(yes_button(transport)))
    assert order[0][0] == "answer" and order[1][0] == "approve", "Telegram expires unanswered taps within seconds"
    assert [o for o in order if o[0] == "answer"] == [order[0]], "answered exactly once"


class BudgetRecorder(ScriptedReasoner):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.budgets = {}

    def complete_json(self, system, user, max_tokens=None):
        self.budgets[system[:12]] = max_tokens
        return super().complete_json(system, user, max_tokens)


def test_e2_short_calls_get_small_output_budgets_and_matthew_hears_progress(work, store):
    reasoner = BudgetRecorder()
    d, transport = daemon(work, store, reasoner)
    d.handle(text("build the thing"))
    assert reasoner.budgets[CLASSIFY_SYSTEM[:12]] <= 256
    assert any("Drafting a card" in m["text"] for m in transport.sent), "no silent minutes while Qwen drafts"
    d.handle(button(yes_button(transport)))
    assert reasoner.budgets[DECOMPOSE_SYSTEM[:12]] >= 1024
    assert any("Setting up" in m["text"] for m in transport.sent)


def test_e3_decomposition_prompt_asks_for_the_smallest_parallel_plan():
    prompt = DECOMPOSE_SYSTEM.format(max=12)
    assert "smallest" in prompt and "only when" in prompt


class TaggedPlan(ScriptedReasoner):
    def complete_json(self, system, user, max_tokens=None):
        if system.startswith(DECOMPOSE_SYSTEM[:12]):
            return {"tasks": [
                {"goalId": "g1", "title": "Write code", "objective": "o", "acceptance": "a", "dependsOn": []},
                {"goalId": "g2", "title": "Publish the package to PyPI", "objective": "Release v1", "acceptance": "on PyPI", "dependsOn": [0]},
                {"goalId": "g2", "title": "Email the beta users", "objective": "Tell them", "acceptance": "sent", "dependsOn": [], "reservedAction": "external_contact"},
                {"goalId": "g2", "title": "Update docs", "objective": "Docs", "acceptance": "done", "dependsOn": [], "reservedAction": "launch_missiles"},
            ]}
        return super().complete_json(system, user, max_tokens)


def test_e4_reserved_steps_are_tagged_by_model_or_by_code(work, store):
    d, transport = daemon(work, store, TaggedPlan())
    d.handle(text("build the thing"))
    d.handle(button(yes_button(transport)))
    by_title = {t["title"]: t["spec"]["reservedAction"] for t in work["operator"].snapshot()["task"]}
    assert by_title["Publish the package to PyPI"] == "publish_push_deploy", "untagged by the model, caught by code"
    assert by_title["Email the beta users"] == "external_contact", "tagged by the model"
    assert by_title["Write code"] is None
    assert by_title["Update docs"] == "publish_push_deploy", "an invented action name still means 'reserved' (E1): lean reserved, one tap to clear"


def test_e4_keyword_inference_defaults_to_reserved_when_unsure():
    assert infer_reserved("Deploy the service to production", "") == "publish_push_deploy"
    assert infer_reserved("Restart the systemd unit", "") == "live_fleet_config"
    assert infer_reserved("Post the announcement on Slack", "") == "external_contact"
    assert infer_reserved("Write unit tests", "cover the parser") is None


def test_e2_keyword_backstop_covers_the_reviewers_misses():
    reserved = ["Push the branch to GitHub", "Merge the PR into main", "Open a pull request upstream", "Rotate the API key",
                "Update DNS records", "Apply the Terraform plan", "systemctl --user restart metroplex", "Roll out v2 to users",
                "Tag and release 1.2", "docker push the image", "Reply to the customer thread", "Send the newsletter"]
    missed = [p for p in reserved if infer_reserved(p, "") is None]
    assert missed == [], missed
    assert infer_reserved("Write the parser", "", "released package visible on PyPI") == "publish_push_deploy", "acceptance text is checked too"
    for safe in ("Write unit tests", "Refactor the parser", "Add type hints", "Document the CLI flags"):
        assert infer_reserved(safe, "") is None, safe


class InventedTag(ScriptedReasoner):
    def complete_json(self, system, user, max_tokens=None):
        if system.startswith(DECOMPOSE_SYSTEM[:12]):
            return {"tasks": [{"goalId": "g1", "title": "Roll it forward", "objective": "o", "acceptance": "a", "dependsOn": [], "reservedAction": "deploy"},
                              {"goalId": "g2", "title": "Docs", "objective": "o", "acceptance": "a", "dependsOn": []}]}
        return super().complete_json(system, user, max_tokens)


def test_e1_an_invented_reserved_tag_leans_toward_reserved(work, store):
    d, transport = daemon(work, store, InventedTag())
    d.handle(text("build the thing"))
    d.handle(button(yes_button(transport)))
    by_title = {t["title"]: t["spec"]["reservedAction"] for t in work["operator"].snapshot()["task"]}
    assert by_title["Roll it forward"] == "publish_push_deploy", "the model signalled 'reserved'; an unknown name must not erase that"
    assert by_title["Docs"] is None


def test_e3_a_plan_stored_before_the_fix_is_backstopped_on_replay(work, store):
    d, transport = daemon(work, store, ScriptedReasoner())
    d.handle(text("build the thing"))
    card_id = yes_button(transport).split(":")[1]
    card = store.get_card(card_id)
    project_id = "p-legacy-plan"
    store.set(f"plan:{project_id}", [{"goalId": "g1", "title": "Publish the package to PyPI", "objective": "o", "acceptance": "a", "dependsOn": []},
                                     {"goalId": "g2", "title": "Docs", "objective": "o", "acceptance": "a", "dependsOn": []}])
    work.cmd("cos", "approval.record", "ap-legacy", {"scope": "project", "cardHash": card["card_hash"], "fromId": "7001", "chatId": "7001", "messageId": "1"})
    work.cmd("cos", "project.create", project_id, {"card": card["card"], "approvalId": "ap-legacy"})
    d.intake.decompose(project_id, card["card"])
    by_title = {t["title"]: t["spec"]["reservedAction"] for t in work["operator"].snapshot()["task"]}
    assert by_title["Publish the package to PyPI"] == "publish_push_deploy"


def test_backlog_skip_reports_how_many_messages_were_skipped():
    from cos.bot import Bot
    from tests.conftest import MATTHEW
    def transport(method, payload):
        if method == "getWebhookInfo":
            return {"url": "", "pending_update_count": 3}
        if method == "getUpdates":
            return [{"update_id": 9, "message": {"message_id": 1, "text": "x", "from": {"id": MATTHEW}, "chat": {"id": MATTHEW}}}]
        return True
    assert Bot(transport, MATTHEW, (MATTHEW,)).skip_backlog() == (10, 3)
