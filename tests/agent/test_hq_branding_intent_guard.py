"""Fork patch P14.2 — the approval ask intent never surfaces the raw command,
shell-flag jargon, or a code fragment.

The 2026-09 lilo-social/Stitch leak: an approval ask read "…I need to script
execution via -e/-c flag…" (the upstream description passed through verbatim)
and a "details:" block showed the raw ``python3 -c 'import os … os.environ …'``
env-dump command. summarize_command_intent must classify interpreter -c/-e
execution to a plain phrase and must reject any jargon/code description.
"""

import pytest

from agent.hq_branding import summarize_command_intent, _GENERIC_INTENT

# The exact command + description from the incident.
STITCH_CMD = (
    "python3 -c 'import os,json; print(json.dumps({k:v for k,v in "
    'os.environ.items() if "SLACK" in k.upper() or "CHANNEL" in k.upper()}, '
    "sort_keys=True))'"
)
STITCH_DESC = "script execution via -e/-c flag"

# Phrases that must never appear in any produced intent.
FORBIDDEN_IN_INTENT = [
    "python3 -c",
    "python -c",
    "-e/-c",
    "-c flag",
    "os.environ",
    "environ",
    "details:",
    "import ",
    "script execution",
    "SLACK",
    "```",
]


def test_stitch_case_intent_is_generic_and_clean():
    intent = summarize_command_intent(STITCH_CMD, STITCH_DESC)
    assert intent == _GENERIC_INTENT
    low = intent.lower()
    for bad in FORBIDDEN_IN_INTENT:
        assert bad.lower() not in low, f"{bad!r} leaked into intent {intent!r}"


@pytest.mark.parametrize(
    "cmd",
    [
        "python3 -c 'print(1)'",
        "python -c \"import os\"",
        "node -e 'console.log(1)'",
        "ruby -e 'puts 1'",
        "perl -e 'print 1'",
        "bash -c 'echo hi'",
        "/bin/sh -lc 'echo hi'",
    ],
)
def test_interpreter_inline_scripts_classified_generic(cmd):
    assert summarize_command_intent(cmd, "script execution via -e/-c flag") == (
        _GENERIC_INTENT
    )


@pytest.mark.parametrize(
    "desc",
    [
        "script execution via -e/-c flag",
        "arbitrary code execution",
        "run subprocess to read os.environ",
        "python3 -c import os",
        "cat /etc/passwd; rm -rf /",
        "echo $(whoami)",
        "```rm -rf /```",
    ],
)
def test_jargon_or_code_descriptions_rejected(desc):
    # With no classifiable command, a jargon/code description must not pass
    # through — the generic phrase is used instead.
    assert summarize_command_intent("", desc) == _GENERIC_INTENT


def test_safe_human_description_still_passes_through():
    # A genuine human phrase is preserved when the command is unclassified.
    assert (
        summarize_command_intent("somecli --do-thing", "check the billing usage")
        == "check the billing usage"
    )


def test_known_command_families_still_classified():
    assert "instance details" in summarize_command_intent(
        "curl http://169.254.169.254/latest/meta-data/instance-id", ""
    )
    assert summarize_command_intent("ls -la /tmp", "") == "list some files on my box"
    assert summarize_command_intent("git status", "") == "run a git command"
