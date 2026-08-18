#!/usr/bin/env bash
# hq-debrand-verify.sh — grep the runtime-visible surface for Hermes leaks.
#
# Scans exactly what an AI agent running INSIDE the runtime can observe about
# its own identity — the same file set hq-debrand.sh transforms: the Python
# packages, the `hqr` entry script, shell / systemd / plist templates, docker
# hooks, locale catalogs, plugin + MCP manifests, and runtime-loaded skill /
# plugin markdown. It does NOT scan surfaces the in-runtime agent never sees
# (website/, docs/, tests/, evals/, apps/ desktop app, web/, nix/, installers
# under scripts/*.ps1, CI under .github/, dotfiles, build files, lockfiles).
#
# Allow-lists strings that must stay "hermes" for functional reasons:
#   - upstream URLs / external plugin repos (nousresearch, github, pypi, ...)
#   - model IDs (Hermes-4*, hermes3*), wakeword assets (hey_hermes)
#   - camelCase IPC/JS globals shared with the untransformed desktop app
#     (hermesPath / hermesHome / hermesDesktop / hermesManaged)
#   - telemetry schema contract (hermes_version, shared_metrics, urn:hermes-agent)
#   - coincidental substrings (ephermeral) and the debrand tooling markers
#
# Exit 0 = clean. Exit 1 = leaks found (listed on stdout).
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

# Also allow-listed:
#   HERMESPENTEST : unique command-injection sentinel emitted by the web-pentest
#                   skill (a probe marker, renaming would not de-brand anything)
#   nadyahermes / hermesagent26 / anonaddy : real contributor email handles in
#                   the release.py PR-attribution table (real identities)
ALLOW='nousresearch|github\.com|githubusercontent|pypi\.org|huggingface|hey_hermes|Hermes-4|hermes3|hermesPath|hermesHome|hermesDesktop|hermesManaged|hermes_version|shared_metrics|urn:hermes-agent|ephermeral|r/hermesagent|HERMESPENTEST|nadyahermes|hermesagent26|anonaddy|hq-debrand|debrand-compat'

# Runtime-surface file set — mirrors hq-debrand.sh's content pass.
PRUNE=( -path ./website -o -path ./.git -o -path ./node_modules -o -path ./ui-tui \
        -o -path ./tests-js -o -path ./tools/wakewords -o -path ./contributors \
        -o -path ./docs -o -path ./assets -o -path ./mcp-research-data \
        -o -path ./tests -o -path ./evals -o -path ./apps -o -path ./web -o -path ./nix )

# find | xargs pipe (bash 3.2 compatible — no mapfile). The trailing /dev/null
# sentinel guarantees grep always has a file arg, so it never blocks on stdin
# when the file list is empty, and -H is always honored.
hits="$(find . \( "${PRUNE[@]}" \) -prune -o -type f \( \
      -name '*.py' -o -name '*.sh' -o -name '*.service' -o -name '*.plist' \
      -o -name 'pyproject.toml' -o -name 'setup.py' -o -name 'Dockerfile*' \
      -o -name 'docker-compose*.yml' -o -name '.env.example' -o -name '.envrc' \
      -o -name 'cli-config.yaml.example' -o -name 'hqr' \
      -o -path './locales/*.yaml' \
      -o -path './skills/*.md' -o -path './optional-skills/*.md' -o -path './plugins/*.md' \
      -o -path './plugins/*/plugin.yaml' -o -path './optional-mcps/*/manifest.yaml' \
      -o -path './docker/*' \
    \) -print0 \
  | xargs -0 grep -niIH hermes /dev/null 2>/dev/null | grep -Ev "$ALLOW" || true)"

if [ -n "$hits" ]; then
  echo "LEAKS FOUND ($(printf '%s\n' "$hits" | wc -l | tr -d ' ') lines):"
  printf '%s\n' "$hits"
  exit 1
fi
echo "clean: no runtime-visible hermes references outside the allow-list"
exit 0
