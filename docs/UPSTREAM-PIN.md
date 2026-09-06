# Upstream pin & fork-patch inventory (`hq/v2`)

This fork (`indigoai-us/hermes-agent`, branch `hq/v2`) tracks upstream
`NousResearch/hermes-agent` but carries a small set of **fork patches** —
numbered `P##` — that must survive rebases onto new upstream. Each patch is
tagged in-source with a `Fork patch P##` comment so it is greppable
(`git grep "Fork patch P"`), and every patch is gated behind a config flag that
defaults **off**, so a stock (non-HQ) checkout behaves byte-for-byte like
upstream.

When rebasing onto a new upstream pin, re-verify every row below still applies
and its tests pass.

## Inventory

| Patch | Area | Summary | Primary location | Config gate |
|-------|------|---------|------------------|-------------|
| P10 | Slack | Shared/forwarded message `message_blocks` mining. | `plugins/platforms/slack/adapter.py` | — |
| P11 | Slack | Restart-free bot-token reload. Socket Mode authenticates with the non-rotating app token, so a rotated bot token only invalidates the Web API clients; swap the token onto the live Web clients in place (no restart, no socket drop). | `plugins/platforms/slack/adapter.py` (`_reload_bot_token`, `_maybe_reload_bot_token_from_watchdog`) | `platforms.slack.bot_token_reload_enabled` (default off) |
| **P11.1** | **Slack** | **Single-source bot-token provider.** P11 only swapped the token on `app.client` + `_team_clients`; any other Web client the gateway held (channel_directory sweep, thread-follow, file upload, users lookup) that captured the startup token kept 401ing with `token_expired` after a rotation until a full restart, while `gateway_state` still reported Slack connected. P11.1 introduces `_SlackBotTokenStore` as the ONE source of truth — every Web client is built via `_make_web_client` and reads its token from the store at call time, so a rotation is one atomic store update no client can miss. Also: `token_expired` now arms the reactive reload (it previously did not), and Web-API auth failures report `slack: degraded (token_expired)` to gateway runtime status (recovering to `connected` after a successful reload). | `plugins/platforms/slack/adapter.py` (`_SlackBotTokenStore`, `_ReloadableAsyncWebClient`, `_make_web_client`, `_reload_bot_token`, `_describe_slack_api_error`, `_note_slack_auth_health`) | Inherits `platforms.slack.bot_token_reload_enabled`; degraded reporting is unconditional (observability only) |
| P12 | Slack | Persisted thread-follow — durably record threads the bot has replied in so follow-ups survive a gateway restart / in-memory miss. | `plugins/platforms/slack/adapter.py` (`_followed_threads`, `_persist_followed_threads`) | `platforms.slack.thread_follow_replies` |
| P13 | Gateway | Per-turn system-notice master gate + shared-channel steer gate. | `gateway/run.py`, `gateway/config.py` | `gateway.system_notices_enabled` |
| P14 | Gateway | Approval prompts speak as a person, fold the raw command, route external channels privately. | `gateway/run.py`, `agent/hq_branding.py` | `gateway.approval_voice_enabled` |
| P16 | Gateway | Persistent pending clarifies — a clarify request survives a gateway restart and its late reply is routed back to the resumed turn. | `gateway/run.py` | — |
| P18 | Agent | SOUL/persona-change system-prompt invalidation for ALL sessions. Stock hermes stamps + re-checks the capability epoch only on Bot Chat prompts, so a continuing non-bot session never adopted a SOUL.md edit until a restart / `/new` / compression (a bot kept a stale persona across releases). When on, every built prompt carries the capability-epoch stamp (`capability_fingerprint()` hashes SOUL + skills + toolsets + MCP + roster) and the restore path rebuilds once when it drifts; an ordinary stale session is not re-titled "Bot Chat". Unchanged SOUL hashes identically ⇒ stored bytes reused verbatim (prefix cache preserved). | `agent/agent_init.py`, `agent/system_prompt.py`, `agent/conversation_loop.py` | `agent.system_prompt_invalidate_on_soul_change` (default off; hq-agents-v2 template renders it on) |

## Tests

- P11 / P11.1: `tests/gateway/test_slack_bot_token_reload.py`,
  `tests/gateway/test_slack_bot_token_reload_provider.py`
  (`uv run pytest tests/gateway -k slack`).
- The provider test file includes a **source-level tripwire**
  (`test_tripwire_no_captured_token_web_client_instantiations`) that fails if any
  new live-adapter code constructs a Web client from a captured token string
  outside the store-bound `_make_web_client` factory — the class of bug P11.1
  fixes. Route every new Slack Web client through `_make_web_client`.
- P18: `tests/agent/test_system_prompt_soul_invalidation.py`
  (`uv run pytest tests/agent/test_system_prompt_soul_invalidation.py`) — plus the
  hq-agents-v2 template render test that pins the flag renders on.
