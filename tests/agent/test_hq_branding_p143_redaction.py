"""Fork patch P14.3 — approval-details redaction and the forbidden-literal
tripwire.

The command details that could ever reach a chat surface must never carry
runtime/tool internals (``hermes_tools`` / ``execute_code`` / ``terminal(``),
secret variable NAMES (``*_PASSWORD`` / ``*_TOKEN`` / ``*_KEY`` / ``*_SECRET``
/ ``*_USERNAME``), or file paths. The unredacted folded command is delivered
only to the owner's HQ DM. This closes the 2026-09-06 Deacon leak (policy
indigo-fleet-agents-never-broadcast-runtime-lifecycle-messages).
"""

import sys
from pathlib import Path

_repo = str(Path(__file__).resolve().parents[2])
if _repo not in sys.path:
    sys.path.insert(0, _repo)

from agent import hq_branding  # noqa: E402


# The exact command Deacon folded into a shared-channel thread reply.
DEACON_LEAK = (
    "execute_code <<'PY'\n"
    "from hermes_tools import terminal\n"
    'r=terminal("hq secrets --company indigo exec --only '
    "HQ_OPERATOR_API_COGNITO_USERNAME,HQ_OPERATOR_API_COGNITO_PASSWORD "
    '-- node /tmp/hq_feedback_health_snapshot.mjs",timeout=240)\n'
    "PY"
)


class TestForbiddenLiteralDetection:
    def test_flags_tool_internals(self):
        assert hq_branding.contains_forbidden_approval_literal(
            "from hermes_tools import terminal"
        )
        assert hq_branding.contains_forbidden_approval_literal("execute_code foo")
        assert hq_branding.contains_forbidden_approval_literal('terminal("ls")')

    def test_flags_secret_names(self):
        for name in (
            "HQ_OPERATOR_API_COGNITO_PASSWORD",
            "SOME_API_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "SERVICE_KEY",
            "HQ_OPERATOR_API_COGNITO_USERNAME",
        ):
            assert hq_branding.contains_forbidden_approval_literal(
                f"dump {name}"
            ), name

    def test_flags_file_paths(self):
        for path in ("/tmp/x.mjs", "~/secrets.env", "/home/agent/.aws/creds"):
            assert hq_branding.contains_forbidden_approval_literal(
                f"read {path}"
            ), path

    def test_clean_intent_is_not_flagged(self):
        assert (
            hq_branding.contains_forbidden_approval_literal(
                "run a health snapshot on my box"
            )
            is None
        )
        assert (
            hq_branding.contains_forbidden_approval_literal("run a git command")
            is None
        )


class TestRedaction:
    def test_deacon_leak_is_fully_scrubbed(self):
        red = hq_branding.redact_approval_details(DEACON_LEAK)
        assert hq_branding.contains_forbidden_approval_literal(red) is None
        for forbidden in (
            "hermes_tools",
            "execute_code",
            "terminal(",
            "_PASSWORD",
            "_USERNAME",
            "/tmp/",
        ):
            assert forbidden not in red, forbidden

    def test_details_block_output_clears_the_tripwire(self):
        block = hq_branding.approval_details_block(DEACON_LEAK)
        assert block.startswith("details:")
        assert hq_branding.contains_forbidden_approval_literal(block) is None

    def test_redaction_is_fail_safe_on_empty(self):
        assert hq_branding.redact_approval_details("") == ""
        assert hq_branding.redact_approval_details(None) == ""


class TestIntentNeverEchoesSecretName:
    def test_secret_name_in_description_falls_back_to_generic(self):
        # A description that names a secret var must not be echoed into the ask.
        intent = hq_branding.summarize_command_intent(
            "somecli --run", "dump AWS_SECRET_ACCESS_KEY"
        )
        assert intent == hq_branding._GENERIC_INTENT
        assert hq_branding.contains_forbidden_approval_literal(intent) is None

    def test_path_in_description_falls_back_to_generic(self):
        intent = hq_branding.summarize_command_intent(
            "somecli --run", "read /tmp/creds.env"
        )
        assert intent == hq_branding._GENERIC_INTENT
