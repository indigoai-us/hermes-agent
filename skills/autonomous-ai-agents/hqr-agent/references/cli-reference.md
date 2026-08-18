# HQ Runtime CLI Reference

Live sources when anything looks stale: `hqr --help`, `hqr <command> --help`,
https://hermes-agent.nousresearch.com/docs/reference/cli-commands

### Global Flags

```
hqr [flags] [command]        (no subcommand = interactive chat)

  --version, -V             Show version
  -z, --oneshot PROMPT      One-shot: print ONLY the final response (for scripts/pipes)
  -m MODEL  --provider P    Model/provider override for this invocation
  -t, --toolsets LIST       Comma-separated toolsets for this invocation
  --resume, -r SESSION      Resume session by ID or title
  --continue, -c [NAME]     Resume by name, or most recent session
  --worktree, -w            Isolated git worktree mode (parallel agents)
  --skills, -s SKILL        Preload skills (comma-separate or repeat)
  --profile, -p NAME        Use a named profile
  --yolo                    Skip dangerous command approval
  --tui / --cli             Force the Ink TUI / classic REPL
  --ignore-rules            Skip AGENTS.md/SOUL.md/memory/skill injection
  --safe-mode               Disable ALL customizations (troubleshooting)
  --pass-session-id         Include session ID in system prompt
```

### Chat

```
hqr chat [flags]
  -q, --query TEXT          Single query, non-interactive
  --image PATH              Attach a local image to a single query
  -Q, --quiet               Suppress banner, spinner, tool previews
  --checkpoints             Enable filesystem checkpoints (/rollback)
  --max-turns N             Cap tool-calling iterations
  --source TAG              Session source tag (default: cli)
```
(plus the global flags above)

### Configuration

```
hqr setup [section]      Wizard (model|tts|terminal|gateway|tools|agent)
hqr model                Interactive model/provider picker
hqr fallback [add|remove|list]  Fallback provider chain
hqr config [show|edit|get|set|unset|path|env-path|check|migrate]
hqr login / logout       OAuth sign-in / clear stored auth
hqr doctor [--fix]       Check dependencies and config
hqr status [--all]       Component status
```

### Tools & Skills

```
hqr tools [list|enable NAME|disable NAME]   Per-platform toolsets (curses UI with no args)

hqr skills list|browse|search QUERY|inspect ID
hqr skills install ID    Hub identifier OR a direct https://…/SKILL.md URL
hqr skills config        Enable/disable skills per platform
hqr skills check|update|uninstall|publish PATH
hqr skills tap add REPO  Add a GitHub repo as a skill source
hqr bundles              Skill bundles (one /<name> alias loads several skills)
```

### MCP Servers

```
hqr mcp add NAME (--url or --command) | remove | list | test NAME
hqr mcp catalog | install NAME     Curated catalog install
hqr mcp configure NAME             Toggle tool selection
hqr mcp serve                      Run HQ Runtime as an MCP server
```
Details (transport, tool discovery, catalog): `references/native-mcp.md`.

### Gateway (Messaging Platforms)

```
hqr gateway run|install|start|stop|restart|status|setup
```

20+ platforms: Telegram, Discord, Slack, WhatsApp (Baileys + Business Cloud API), iMessage (Photon — `hqr photon setup`), Signal, Email, SMS, Matrix, Mattermost, Teams, LINE, SimpleX, ntfy, Google Chat, Home Assistant, DingTalk, Feishu, WeCom, Weixin, API Server, Webhooks. Open WebUI connects via the API Server adapter. Most adapters ship under `plugins/platforms/`.
Docs: https://hermes-agent.nousresearch.com/docs/user-guide/messaging/

### Sessions

```
hqr sessions list|browse|rename ID TITLE|delete ID|export OUT|prune|stats
```

### Cron / Webhooks

```
hqr cron list|create SCHED|edit ID|pause|resume|run ID|remove|status
    Schedules: '30m', 'every 2h', '0 9 * * *', ISO timestamp
hqr webhook subscribe NAME|list|remove NAME|test NAME
```
Webhook payloads/routes: `references/webhooks.md`.

### Profiles

```
hqr profile list|create NAME (--clone|--clone-all|--clone-from)|use|show|delete
hqr profile rename A B | alias NAME | export NAME | import FILE
```

### Credentials & Pools

```
hqr auth                 Interactive credential manager
hqr auth add [PROVIDER]  Add OAuth or API-key credential (nous, openai-codex, qwen-oauth, …)
hqr auth list|remove P IDX|reset PROVIDER|status
```
Multiple credentials per provider form a pool that rotates automatically and skips exhausted keys.

### Other

```
hqr desktop / gui        Native desktop app
hqr dashboard            Web admin panel + embedded chat (--stop / --status)
hqr proxy                OpenAI-compatible local proxy backed by an OAuth provider
hqr portal               Quick setup / sign in via Nous Portal
hqr kanban <verb>        Multi-agent work-queue board
hqr project              Named multi-folder workspaces
hqr skin list|use|set    Switch/tweak skins (see references/themes.md)
hqr pets <verb>          Pet mascots (see references/petdex.md)
hqr memory setup|status|off|reset   Memory provider
hqr secrets bitwarden|onepassword   External secret stores
hqr moa                  Mixture-of-Agents slots
hqr hooks / security / backup / import / checkpoints / console
hqr logs [-f] [errors]   View agent/error logs
hqr send                 One-off message through a gateway platform
hqr pairing / plugins / insights / journey / computer-use
hqr acp                  ACP server (IDE integration)
hqr completion bash|zsh|fish
hqr update / uninstall / claw migrate
```

Plugin- and provider-supplied subcommands (e.g. `hqr photon setup`) only appear once their plugin is installed/active.

### Where to Find Things

| Looking for... | Location |
|---|---|
| Config options | `hermes config edit` · [Configuration docs](https://hermes-agent.nousresearch.com/docs/user-guide/configuration) |
| Tools / toolsets | `hermes tools list` · [Tools reference](https://hermes-agent.nousresearch.com/docs/reference/tools-reference) |
| Skills catalog | `hermes skills browse` · [Skills catalog](https://hermes-agent.nousresearch.com/docs/reference/skills-catalog) |
| Provider setup | `hermes model` · [Providers guide](https://hermes-agent.nousresearch.com/docs/integrations/providers) |
| Env variables | `hermes config env-path` · [Env vars reference](https://hermes-agent.nousresearch.com/docs/reference/environment-variables) |
| Gateway logs | `~/.hqr/logs/gateway.log` (or `hqr logs`) |
| Sessions | `hqr sessions browse` (reads state.db) |
