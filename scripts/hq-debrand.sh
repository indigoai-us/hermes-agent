#!/usr/bin/env bash
# hq-debrand.sh — mechanical, idempotent Hermes -> HQ Runtime de-brand transform.
#
# Renames the runtime-visible Hermes surface to the HQ Runtime naming scheme:
#   runtime name        : "Hermes" / "Hermes Agent"  -> "HQ Runtime"
#   binary / launcher   : hermes                     -> hqr
#   python packages     : hermes_*                   -> hqr_*   (real git mv, so tracebacks show hqr_*)
#   env var prefix      : HERMES_*                   -> HQR_*
#   default home dir    : ~/.hermes                  -> ~/.hqr
#   identifiers         : HermesFoo / Hermes_Foo     -> HqrFoo / Hqr_Foo
#   snake_case ids      : as_hermes / _to_hermes     -> as_hqr / _to_hqr
#   HTTP-ish tokens     : Hermes-Session-Id etc.     -> Hqr-Session-Id
#   command regexes     : r"\bhermes\s+gateway"      -> r"\bhqr\s+gateway"
#
# Deliberately NOT touched (GUARD substrings + the prune list):
#   - website/                  (never visible to the running agent)
#   - ui-tui/ (JS workspace), node_modules, tests-js, apps/ (desktop app),
#     web/, nix/, docs/, assets/, contributors/, mcp-research-data/
#   - upstream URLs (nousresearch.com, github.com, githubusercontent, pypi,
#     huggingface) -> left byte-identical so the self-updater keeps working
#   - model-id strings (Hermes-4..., hermes3:70b) -> renaming breaks inference
#   - wakeword assets hey_hermes.{tflite,onnx} and their references
#   - camelCase IPC keys shared with the untransformed desktop app
#     (hermesPath / hermesHome) are preserved because an alnum follows "hermes"
#
# Idempotent: a second run finds nothing to rename and no tokens to replace.
# Re-runnable against future upstream tags: purely pattern-driven, no hardcoded file list.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Lines containing any of these are left completely untouched by the word-level pass.
GUARD='nousresearch|github\.com|githubusercontent|pypi\.org|huggingface|hey_hermes|Hermes-4|hermes3|hq-debrand'

# Path prunes for the content pass (works because that find has no -depth).
# The debrand tooling itself is excluded so it never rewrites its own patterns.
PRUNE=( -path ./website -o -path ./.git -o -path ./node_modules -o -path ./ui-tui \
        -o -path ./tests-js -o -path ./tools/wakewords -o -path ./contributors \
        -o -path ./docs -o -path ./assets -o -path ./mcp-research-data \
        -o -path ./scripts/hq-debrand.sh -o -path ./scripts/hq-debrand-verify.sh )

echo "== [1/2] rename pass (git mv) =="
# -depth: children before parents, so nested hermes-named entries move first
# (a dir and a file under it can both match *hermes*). NB: -prune is a no-op
# under -depth, so exclusions here MUST be ! -path negations, not -prune.
# Scoped to the runtime surface; the desktop app / installers under apps/,
# web/, nix/ and the docs site under website/ keep their own naming.
find . -depth -name '*hermes*' \
       ! -path './website/*'   ! -path './.git/*' ! -path './node_modules/*' \
       ! -path './ui-tui/*'    ! -path './tests-js/*' ! -path './contributors/*' \
       ! -path './docs/*'      ! -path './assets/*'   ! -path './mcp-research-data/*' \
       ! -path './apps/*'      ! -path './web/*'      ! -path './nix/*' \
       ! -path './tools/wakewords/*' \
       -print0 |
while IFS= read -r -d '' p; do
  base="$(basename "$p")"
  new="$(dirname "$p")/${base//hermes/hqr}"
  [ "$p" = "$new" ] && continue
  echo "  git mv $p -> $new"
  git mv "$p" "$new"
done

echo "== [2/2] content pass (perl) =="
# File set: python + shell + service/unit templates + packaging + container/env/config
# templates + locale catalogs + skill/plugin markdown loaded into agent context.
find . \( "${PRUNE[@]}" \) -prune -o -type f \( \
      -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.plist' \
      -o -name 'pyproject.toml' -o -name 'setup.py' -o -name 'Dockerfile*' \
      -o -name 'docker-compose*.yml' -o -name '.env.example' -o -name '.envrc' \
      -o -name 'cli-config.yaml.example' -o -name 'hqr' \
      -o -path './locales/*.yaml' \
      -o -path './skills/*.md' -o -path './optional-skills/*.md' -o -path './plugins/*.md' \
      -o -path './plugins/*/plugin.yaml' -o -path './optional-mcps/*/manifest.yaml' \
      -o -path './docker/*' \
    \) -print0 |
xargs -0 perl -pi -e '
  BEGIN { $g = q{'"$GUARD"'}; }
  # Identifier-shaped renames run on EVERY line, guarded or not: these token
  # shapes never occur inside the protected URL / model-id substrings, and a
  # guarded line can legitimately mix an identifier with a protected URL
  # (e.g. `HERMES_INDEX_URL = "https://hermes-agent.nousresearch.com/..."`).
  s/hermes_/hqr_/g;                        # package/module/attr names (traceback surface)
  s/HERMES_/HQR_/g;                        # env var prefix
  s/Hermes(?=[A-Z_0-9])/Hqr/g;             # CamelCase + underscore identifiers
  s/Hermes-(?=[A-Z])/Hqr-/g;               # HTTP-header-style tokens (not Hermes-4 model IDs)
  # CLI-command token inside a regex pattern, e.g. r"\bhermes\s+gateway" in the
  # gateway-lifecycle approval / cron guard rules. Preceded by a literal \b
  # escape, so the boundary rules below never see it. Only occurs as a matcher
  # for the renamed command, so it must track the rename to keep firing.
  s/\\bhermes/\\bhqr/g;                    # regex command token: \bhermes... -> \bhqr...
  # Browser-injected JS page globals (window.__hermesMeetQueue, __hermesDialog…).
  # The double-underscore prefix makes these distinct from the camelCase IPC
  # keys we must preserve (hermesPath/hermesHome have no leading __), so this is
  # safe. They are self-defined+read within one injected script → consistent.
  s/__hermes/__hqr/g;                      # window.__hermes* browser globals
  # Looser word-level renames skip guarded lines so URL values, model IDs and
  # wakeword asset names stay byte-identical.
  unless (m/$g/) {
    s/\.hermes(?![A-Za-z0-9_])/.hqr/g;     # dot-home tokens: ~/.hermes, .hermes.md, ai.hermes.gateway
    # Underscore/dot-boundary (NOT perl \b, which treats _ as a word char):
    # catches `openclaw_to_hermes.py` (whose file the rename pass already moved)
    # and internal snake_case ids, while camelCase IPC keys (hermesPath) and
    # model slugs (hermes3) — where an alnum follows "hermes" — are preserved.
    s/(?<![A-Za-z0-9])hermes(?![A-Za-z0-9])/hqr/g;   # command / paths / dist / snake_case ids
    s/(?<![A-Za-z0-9])HERMES(?![A-Za-z0-9])/HQR/g;   # shouty snake_case ids (e.g. _TO_HERMES)
    # Command / brand immediately after an escaped newline or CR inside a string
    # literal, e.g. f"...\r\nhermes -p" (generated Windows wrapper) or
    # f"\nHermes relaunch failed" (runtime error text). The escaped \n/\r leaves
    # a literal alnum ("n"/"r") before the token, so the boundary rules miss it.
    s/(\\[rn])hermes(?![A-Za-z0-9])/${1}hqr/g;       # \nhermes -p -> \nhqr -p
    s/(\\[rn])Hermes\b/${1}HQ Runtime/g;             # \nHermes relaunch -> \nHQ Runtime relaunch
    s/\bHermes\b/HQ Runtime/g;             # prose brand name
  }
'

echo "done. Review with: git -C $REPO status"
