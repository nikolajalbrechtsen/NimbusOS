#!/bin/bash
# =============================================================================
# tools/lint.sh: the repository's static checks (no network, no root)
# =============================================================================
# What: 1. shellcheck on every tracked shell script: *.sh files and any file
#          whose first line is a sh or bash shebang (hooks, maintainer
#          scripts, tests), with the settings in .shellcheckrc;
#       2. ruff on every Python file, with ruff.toml (skipped with a note if
#          ruff isn't installed; ci.yml always installs it);
#       3. `node --check` on the Control Center's JavaScript;
#       4. every tracked *.json file parses, and every *.yml and *.yaml file
#          (workflows, Dependabot) parses as YAML;
#       5. tools/check-file-modes.sh: git's executable bits match each
#          file's role;
#       6. tools/fetch-docker-key.sh --verify-local: Docker's committed key
#          and its fingerprint comments (docs/PLAN.md §9, decision 3).
#       Runs every check, then exits 1 if any failed.
# Why:  ci.yml's lint job runs exactly this, so a contributor can run the
#       same checks before pushing (spec 4.3: shellcheck and ruff in CI).
#
# Usage: tools/lint.sh
#        Needs git, shellcheck, python3, python3-yaml, nodejs and gpg (Debian:
#        tools/dev-shell.sh); ruff from `pipx install ruff==<ci.yml's pin>`.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
failed=()
section() { printf '\n== %s\n' "$1"; }
run() {   # run NAME COMMAND...: run a check, remember a failure, keep going
	local name="$1"; shift
	if "$@"; then printf '  ok    %s\n' "$name"; else printf '  FAIL  %s\n' "$name"; failed+=("$name"); fi
}

section "shellcheck $(shellcheck --version | sed -n 's/^version: //p')"
mapfile -t shell_files < <(git ls-files -z | while IFS= read -r -d '' f; do
	[[ -f "$f" && ! -L "$f" ]] || continue
	case "$f" in
		*.sh) printf '%s\n' "$f" ;;
		*) head -n 1 "$f" | grep -Eq '^#! ?(/usr)?/bin/(env )?(ba)?sh( |$)' && printf '%s\n' "$f" ;;
	esac
done)
echo "  ${#shell_files[@]} scripts"
run "shellcheck" shellcheck --format=gcc "${shell_files[@]}"

section "ruff"
if command -v ruff >/dev/null; then
	ruff --version
	run "ruff check" ruff check --no-cache --output-format concise .
else
	echo "  note  ruff isn't installed here; ci.yml runs it (pipx install ruff==<pinned>)"
fi

section "JavaScript"
mapfile -t js_files < <(git ls-files 'control-center/frontend/*.js')
js_ok() { local f; for f in "$@"; do node --check "$f" || return 1; done; }
run "node --check (${#js_files[@]} files)" js_ok "${js_files[@]}"

section "JSON"
mapfile -t json_files < <(git ls-files '*.json')
json_ok() { python3 -c 'import json, sys
bad = 0
for path in sys.argv[1:]:
    try:
        json.load(open(path, encoding="utf-8"))
    except ValueError as exc:
        print(f"  {path}: {exc}"); bad += 1
sys.exit(1 if bad else 0)' "$@"; }
run "every JSON file parses (${#json_files[@]} files)" json_ok "${json_files[@]}"
mapfile -t yaml_files < <(git ls-files '*.yml' '*.yaml')
yaml_ok() { python3 -c 'import sys, yaml
for path in sys.argv[1:]:
    yaml.safe_load(open(path, encoding="utf-8"))' "$@"; }
run "every YAML file parses (${#yaml_files[@]} files)" yaml_ok "${yaml_files[@]}"

section "repository rules"
run "git file modes match their roles" tools/check-file-modes.sh
run "Docker's committed key and fingerprint comments" tools/fetch-docker-key.sh --verify-local

echo
if ((${#failed[@]})); then
	echo "lint: ${#failed[@]} check(s) failed: ${failed[*]}"
	exit 1
fi
echo "lint: all checks passed"
