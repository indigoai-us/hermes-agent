import pytest

from agent import hq_branding
from agent.prompt_builder import (
    DEFAULT_AGENT_IDENTITY,
    HERMES_AGENT_HELP_GUIDANCE,
    HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS,
)

OFF = {"agent": {"hq_branding": False}}
ON = {"agent": {"hq_branding": True}}


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(hq_branding.DISPLAY_NAME_ENV, raising=False)


class TestFlagOffIsStock:
    def test_agent_name(self):
        assert hq_branding.agent_name(OFF) == "Hermes"

    def test_interrupt_notice(self):
        assert hq_branding.interrupt_notice(False, OFF) == (
            "⚠️ Gateway shutting down — Your current task will be interrupted."
        )
        assert hq_branding.interrupt_notice(True, OFF) == (
            "⚠️ Gateway restarting — Your current task will be interrupted. "
            "Send any message after restart and I'll try to resume where you left off."
        )

    def test_busy_notice(self):
        assert hq_branding.busy_notice(True, queued=True, config=OFF) == (
            "⏳ Gateway restarting — queued for the next turn after it comes back."
        )
        assert hq_branding.busy_notice(True, config=OFF) == (
            "⏳ Gateway is restarting and is not accepting another turn right now."
        )
        assert hq_branding.busy_notice(False, new_work=True, config=OFF) == (
            "⏳ Gateway is shutting down and is not accepting new work right now."
        )

    def test_online_notices(self):
        assert hq_branding.back_online_notice(OFF) == "♻️ Gateway online — Hermes is back and ready."
        assert hq_branding.restarted_notice(OFF) == (
            "♻ Gateway restarted successfully. Your session continues."
        )

    def test_cron_notice(self):
        assert hq_branding.cron_interrupt_notice("nightly", False, OFF) == (
            "⚠️ Cron job 'nightly' was interrupted — the gateway is shutting down "
            "and killed the run before it finished. No result was produced for this run."
        )

    def test_update_notices(self):
        assert hq_branding.update_finished_notice(config=OFF) == "✅ Hermes update finished."
        assert hq_branding.update_finished_notice(True, OFF) == (
            "✅ Hermes update finished successfully."
        )
        assert hq_branding.update_failed_notice(config=OFF) == "❌ Hermes update failed."
        assert hq_branding.update_failed_notice(exit_code=3, config=OFF) == (
            "❌ Hermes update failed (exit code 3)."
        )
        assert hq_branding.update_failed_notice(hint=True, config=OFF) == (
            "❌ Hermes update failed. Check the gateway logs or run "
            "`hermes update` manually for details."
        )
        assert hq_branding.update_timeout_notice(OFF) == (
            "❌ Hermes update timed out after 30 minutes."
        )
        assert hq_branding.update_target_label(OFF) == "Hermes Agent"

    def test_identity_and_help_guidance_untouched(self):
        assert hq_branding.default_agent_identity(DEFAULT_AGENT_IDENTITY, OFF) is DEFAULT_AGENT_IDENTITY
        assert hq_branding.help_guidance(HERMES_AGENT_HELP_GUIDANCE, OFF) is HERMES_AGENT_HELP_GUIDANCE


class TestAgentName:
    def test_display_name_env_wins(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "Ace")
        assert hq_branding.agent_name(ON) == "Ace"

    def test_whitespace_is_stripped(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "  Cherlie  ")
        assert hq_branding.agent_name(ON) == "Cherlie"

    def test_falls_back_to_hq(self):
        assert hq_branding.agent_name(ON) == "HQ"

    def test_blank_env_falls_back_to_hq(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "   ")
        assert hq_branding.agent_name(ON) == "HQ"


class TestFlagOnCopy:
    def test_shutdown_uses_agent_name(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "Ace")
        assert hq_branding.interrupt_notice(False, ON) == (
            "⚠️ Ace is offline for a moment — your current task will stop here."
        )

    def test_restart_invites_a_follow_up(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "Ace")
        assert "Ace is restarting" in hq_branding.interrupt_notice(True, ON)

    def test_busy_variants_are_distinct(self):
        queued = hq_branding.busy_notice(True, queued=True, config=ON)
        busy = hq_branding.busy_notice(True, config=ON)
        new_work = hq_branding.busy_notice(True, new_work=True, config=ON)
        assert len({queued, busy, new_work}) == 3

    def test_no_gateway_jargon_reaches_the_user(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "Ace")
        for text in _all_enabled_copy():
            assert "Gateway" not in text
            assert "gateway" not in text


class TestIdentityLeak:
    def test_identity_drops_the_vendor_sentence(self):
        rendered = hq_branding.default_agent_identity(DEFAULT_AGENT_IDENTITY, ON)
        assert "Hermes" not in rendered
        assert "Nous Research" not in rendered
        assert rendered.startswith(hq_branding.HQ_IDENTITY_LEAD)

    def test_identity_keeps_the_behaviour_spec(self):
        rendered = hq_branding.default_agent_identity(DEFAULT_AGENT_IDENTITY, ON)
        tail = DEFAULT_AGENT_IDENTITY[len(hq_branding.STOCK_IDENTITY_LEAD):]
        assert rendered.endswith(tail)

    def test_unrecognized_upstream_wording_falls_back_clean(self):
        rendered = hq_branding.default_agent_identity(
            "You are Someone Else, built by Another Lab. Be terse.", ON
        )
        assert rendered == hq_branding.HQ_IDENTITY_FALLBACK
        assert "Hermes" not in rendered

    def test_help_guidance_is_suppressed(self):
        assert hq_branding.help_guidance(HERMES_AGENT_HELP_GUIDANCE, ON) == ""
        assert hq_branding.help_guidance(HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS, ON) == ""

    def test_no_vendor_names_in_any_enabled_output(self, monkeypatch):
        monkeypatch.setenv(hq_branding.DISPLAY_NAME_ENV, "Ace")
        for text in _all_enabled_copy():
            assert "Hermes" not in text
            assert "Nous" not in text


def _all_enabled_copy() -> list[str]:
    texts = [
        hq_branding.agent_name(ON),
        hq_branding.back_online_notice(ON),
        hq_branding.restarted_notice(ON),
        hq_branding.default_agent_identity(DEFAULT_AGENT_IDENTITY, ON),
        hq_branding.help_guidance(HERMES_AGENT_HELP_GUIDANCE, ON),
        hq_branding.help_guidance(HERMES_AGENT_HELP_GUIDANCE_NO_SKILLS, ON),
    ]
    texts.extend(
        [
            hq_branding.update_target_label(ON),
            hq_branding.update_finished_notice(config=ON),
            hq_branding.update_finished_notice(True, ON),
            hq_branding.update_failed_notice(config=ON),
            hq_branding.update_failed_notice(exit_code=3, config=ON),
            hq_branding.update_failed_notice(hint=True, config=ON),
            hq_branding.update_timeout_notice(ON),
        ]
    )
    for restarting in (True, False):
        texts.append(hq_branding.interrupt_notice(restarting, ON))
        texts.append(hq_branding.cron_interrupt_notice("nightly", restarting, ON))
        for kwargs in ({"queued": True}, {"new_work": True}, {}):
            texts.append(hq_branding.busy_notice(restarting, config=ON, **kwargs))
    return texts
