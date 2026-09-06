"""Fork patch (hq/v2): the OAuth Claude Code system prefix must not overshadow
the agent's own fleet identity.

Live incident 2026-09-05: a claude-brain fleet agent (Linus) answered its first
DM as "I'm Cherlie…" — a deterministic name confabulation. Root cause: on the
OAuth path ``build_anthropic_kwargs`` prepends the Claude Code prefix ("You are
Claude Code, Anthropic's official CLI for Claude.") as system[0], demoting the
SOUL identity ("You are Linus, an HQ fleet agent…") to system[1]. The same SOUL
worked for codex/grok agents, whose wire carries no Claude Code prefix.

The fix re-asserts the agent's identity as the LAST system block (recency),
without removing the Claude Code prefix the OAuth billing path requires.
"""

_CC_PREFIX = "You are Claude Code, Anthropic's official CLI for Claude."


def _oauth_system(system_text, is_oauth=True):
    from agent.anthropic_adapter import build_anthropic_kwargs

    return build_anthropic_kwargs(
        model="claude-opus-4-8",
        messages=[
            {"role": "system", "content": system_text},
            {"role": "user", "content": "who are you?"},
        ],
        tools=None,
        max_tokens=1024,
        reasoning_config=None,
        is_oauth=is_oauth,
    )["system"]


def test_oauth_fleet_identity_reasserted_as_last_block():
    system = _oauth_system(
        "You are Linus, an HQ fleet agent — a teammate for your company, not a "
        "chatbot.\n\nBe direct."
    )
    assert isinstance(system, list)
    # The Claude Code prefix is still first — OAuth billing/identity is intact.
    assert system[0]["text"] == _CC_PREFIX
    # …but the agent's own identity is re-asserted last so it is authoritative.
    last = system[-1]["text"]
    assert last.startswith("You are Linus, an HQ fleet agent")
    assert '"Claude Code"' in last  # explicit: never answer as Claude Code
    assert "authoritative" in last


def test_oauth_default_identity_is_not_reasserted():
    # The sanitized default identity already reads as Claude Code, so there is
    # no distinct fleet name to protect — nothing extra is appended.
    system = _oauth_system("You are Hermes Agent, built by Nous Research. Be direct.")
    assert isinstance(system, list)
    assert len(system) == 2  # cc prefix + the (sanitized) default identity only
    assert all("HQ fleet agent" not in b.get("text", "") for b in system)


def test_non_oauth_path_is_untouched():
    system = _oauth_system(
        "You are Linus, an HQ fleet agent — a teammate.", is_oauth=False
    )
    # No Claude Code prefix and no identity reassertion off the OAuth path.
    if isinstance(system, list):
        assert all(b.get("text") != _CC_PREFIX for b in system)
        assert all("authoritative" not in b.get("text", "") for b in system)
    else:
        assert system.startswith("You are Linus")


def test_reassert_helper_returns_none_for_claude_code_identity():
    from agent.anthropic_adapter import (
        _CLAUDE_CODE_SYSTEM_PREFIX,
        _oauth_identity_reassert_block,
    )

    system = [
        {"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX},
        {"type": "text", "text": "You are Claude Code, built by Anthropic."},
    ]
    assert _oauth_identity_reassert_block(system) is None


def test_reassert_helper_extracts_only_the_first_identity_line():
    from agent.anthropic_adapter import (
        _CLAUDE_CODE_SYSTEM_PREFIX,
        _oauth_identity_reassert_block,
    )

    system = [
        {"type": "text", "text": _CLAUDE_CODE_SYSTEM_PREFIX},
        {"type": "text", "text": "You are Izzy, an HQ fleet agent — a teammate.\n\nrest of soul"},
    ]
    block = _oauth_identity_reassert_block(system)
    assert block is not None
    assert block["text"].startswith("You are Izzy, an HQ fleet agent — a teammate.")
    assert "rest of soul" not in block["text"]
