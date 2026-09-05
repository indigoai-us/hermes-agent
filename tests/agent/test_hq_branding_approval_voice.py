"""Fork patch P14 — approval-voice copy lives in agent/hq_branding.py.

The approval GATE is unchanged; these cover only the presentation helpers and
the config flag that turns them on. A source tripwire keeps the human-voiced
approval phrases in this one module so a later change cannot scatter a robotic
banner back into an adapter.
"""

from pathlib import Path

from agent import hq_branding
from gateway.config import GatewayConfig


class TestCommandIntentSummary:
    def test_instance_metadata_curl_reads_as_instance_details(self):
        cmd = (
            'TOKEN=$(curl -sS -X PUT "http://169.254.169.254/latest/api/token" '
            '-H "X-aws-ec2-metadata-token-ttl-seconds: 60"); '
            'curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" '
            "http://169.254.169.254/latest/meta-data/instance-id"
        )
        assert hq_branding.summarize_command_intent(cmd) == "read my instance details"

    def test_ls_and_find_read_as_listing(self):
        assert hq_branding.summarize_command_intent("ls -la /tmp") == (
            "list some files on my box"
        )
        assert hq_branding.summarize_command_intent("find / -name x") == (
            "list some files on my box"
        )

    def test_aws_cli_reads_as_aws_api(self):
        assert hq_branding.summarize_command_intent("aws ec2 describe-instances") == (
            "call the AWS API"
        )

    def test_falls_back_to_description_then_generic(self):
        assert hq_branding.summarize_command_intent("", "check the disk usage") == (
            "check the disk usage"
        )
        assert hq_branding.summarize_command_intent("") == (
            "run a quick command on my box"
        )
        # A bare "dangerous command" description is discarded for the generic.
        assert hq_branding.summarize_command_intent(
            "weirdbinary --go", "dangerous command"
        ) == "run a quick command on my box"


class TestAskAndOutcomeText:
    def test_ask_addresses_the_requester(self):
        intent = hq_branding.summarize_command_intent(
            "curl http://169.254.169.254/latest/meta-data/instance-id"
        )
        assert hq_branding.approval_ask_text("Jacob", intent) == (
            "Jacob, to answer that I need to read my instance details. "
            "OK to go ahead?"
        )

    def test_ask_without_a_name_is_still_human(self):
        assert hq_branding.approval_ask_text(None, "list some files on my box") == (
            "To answer that I need to list some files on my box. OK to go ahead?"
        )

    def test_ask_has_no_banner_or_raw_command(self):
        cmd = 'curl "http://169.254.169.254/latest/meta-data/instance-id"'
        ask = hq_branding.approval_ask_text(
            "Jacob", hq_branding.summarize_command_intent(cmd)
        )
        assert "Command Approval Required" not in ask
        assert "169.254.169.254" not in ask
        assert "```" not in ask

    def test_confirmation_and_deny_are_plain(self):
        assert hq_branding.approval_confirmation_text() == "Thanks, running it."
        assert "Approved for session" not in hq_branding.approval_confirmation_text()
        assert hq_branding.approval_denied_text() == (
            "Skipping that check since I didn't get an OK; "
            "here's what I can say without it."
        )

    def test_details_block_folds_the_raw_command(self):
        block = hq_branding.approval_details_block('curl "http://x/meta-data/y"')
        assert block.startswith("details:")
        assert "```" in block
        assert "meta-data" in block


class TestApprovalVoiceFlag:
    def test_default_is_off_stock(self):
        assert GatewayConfig().approval_voice_enabled is False
        assert hq_branding.approval_voice_enabled({}) is False

    def test_top_level_and_nested_config(self):
        assert hq_branding.approval_voice_enabled({"approval_voice_enabled": True})
        assert hq_branding.approval_voice_enabled(
            {"gateway": {"approval_voice_enabled": "true"}}
        )

    def test_config_round_trips(self):
        d = GatewayConfig(approval_voice_enabled=True).to_dict()
        assert d["approval_voice_enabled"] is True
        assert GatewayConfig.from_dict(
            {"approval_voice_enabled": True}
        ).approval_voice_enabled is True


class TestSourceTripwire:
    """The human approval phrases live only in agent/hq_branding.py."""

    def test_phrases_are_not_duplicated_in_adapters_or_gateway(self):
        repo = Path(__file__).resolve().parents[2]
        phrases = [
            "Thanks, running it.",
            "OK to go ahead?",
            "here's what I can say without it.",
        ]
        offenders: list[str] = []
        for rel in (
            "plugins/platforms/slack/adapter.py",
            "gateway/run.py",
        ):
            text = (repo / rel).read_text(encoding="utf-8")
            for phrase in phrases:
                if phrase in text:
                    offenders.append(f"{rel}: {phrase!r}")
        assert not offenders, (
            "P14 approval-voice phrases must live only in agent/hq_branding.py; "
            f"found duplicated literals: {offenders}"
        )
