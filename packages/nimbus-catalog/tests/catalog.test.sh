#!/bin/bash
# =============================================================================
# packages/nimbus-catalog/tests/catalog.test.sh
# =============================================================================
# What: tests the nimbus-catalog packaging with a small test catalog (two
#       apps and a minimal schema, written by this script; the real catalog
#       and schema are deliverable 9):
#         1. debian/check-catalog.py accepts a good catalog and rejects, each
#            with its own message: a missing, string, boolean or zero
#            template_version; an id that doesn't match its directory; a
#            missing compose template; a symbolic link; a schema violation;
#            a stray file; a bad directory name; a missing schema; no apps.
#         2. The package builds (debian/rules runs the check) and installs the
#            apps under /usr/share/nimbus-control/catalog/; it has no
#            maintainer scripts at all.
#         3. Upgrading to a catalog where one app's template changed (with
#            its template_version raised) leaves an installed stack of that
#            app byte for byte unchanged, and "Template update available"
#            (installed manifest newer than the stack's recorded version)
#            is true for exactly that app. Purging the catalog leaves the
#            stack alone too.
#
# Why:  docs/PLAN.md NOTE-60: template updates are offered, never applied by
#       a package upgrade.
#
# Usage: packages/nimbus-catalog/tests/catalog.test.sh
#        Throwaway container only (tools/dev-shell.sh): it builds and
#        installs packages.
# =============================================================================
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$(readlink -f "$0")")/../../.." && pwd -P)"
PKG="$ROOT/packages/nimbus-catalog"
[[ "$(id -u)" -eq 0 ]] || { echo "catalog.test: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] \
	|| { echo "catalog.test: refusing to run outside a container (it installs packages)" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

passed=0
failed=0
check() {   # check NAME COMMAND...
	local name="$1"; shift
	if "$@" >/dev/null 2>&1; then passed=$((passed + 1)); printf '  PASS  %s\n' "$name"
	else failed=$((failed + 1)); printf '  FAIL  %s\n' "$name"; fi
}
WORK="$(mktemp -d)"
CHECK=(python3 "$PKG/debian/check-catalog.py")

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends build-essential python3 python3-jsonschema debhelper dpkg-dev lintian >/tmp/catalog-deps.log 2>&1 \
	|| { tail -n 30 /tmp/catalog-deps.log; exit 1; }

# --- test catalog -------------------------------------------------------------
app() {   # app DIR ID TEMPLATE_VERSION IMAGE
	mkdir -p "$1/$2"
	printf '{\n  "id": "%s",\n  "name": "%s",\n  "category": "monitoring",\n  "template_version": %s\n}\n' "$2" "$2" "$3" >"$1/$2/manifest.json"
	printf 'services:\n  %s:\n    image: {{ "%s" | q }}\n    restart: unless-stopped\n' "$2" "$4" >"$1/$2/docker-compose.yml.j2"
	printf '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="14" fill="#6cc4ff"/></svg>\n' >"$1/$2/icon.svg"
}
make_catalog() {   # make_catalog DIR GRAFANA_VERSION GRAFANA_IMAGE
	mkdir -p "$1"
	cat >"$1/manifest.schema.json" <<'EOF'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["id", "name", "category", "template_version"],
  "properties": {
    "id": {"type": "string"},
    "name": {"type": "string", "minLength": 1},
    "category": {"type": "string"},
    "template_version": {"type": "integer", "minimum": 1}
  }
}
EOF
	printf '# Test catalog schema\n' >"$1/SCHEMA.md"
	app "$1" uptime-kuma 1 "louislam/uptime-kuma:1"
	app "$1" grafana "$2" "$3"
}
variant() {   # variant NAME: a fresh copy of the good catalog to break
	rm -rf "$WORK/$1"
	cp -a "$WORK/good" "$WORK/$1"
	printf '%s' "$WORK/$1"
}
set_field() {   # set_field FILE KEY JSON_VALUE (or "delete")
	python3 - "$@" <<'EOF'
import json, sys
path, key, value = sys.argv[1:4]
data = json.load(open(path))
if value == "delete":
    del data[key]
else:
    data[key] = json.loads(value)
json.dump(data, open(path, "w"))
EOF
}
rejects() {   # rejects DIR TEXT: the check fails and says TEXT
	local out
	if out="$("${CHECK[@]}" "$1" 2>&1)"; then return 1; fi
	grep -qF -- "$2" <<<"$out"
}

make_catalog "$WORK/good" 1 "grafana/grafana:11.3.0"

echo "check-catalog.py"
check "a good catalog passes (2 apps)" sh -c '"$@" | grep -q "2 app(s) OK"' _ "${CHECK[@]}" "$WORK/good"
v="$(variant no-version)"; set_field "$v/grafana/manifest.json" template_version delete
check "missing template_version" rejects "$v" 'grafana/manifest.json: "template_version" must be a whole number of at least 1, got None'
v="$(variant string-version)"; set_field "$v/grafana/manifest.json" template_version '"2"'
check "template_version as a string" rejects "$v" "got '2'"
v="$(variant bool-version)"; set_field "$v/grafana/manifest.json" template_version true
check "template_version true" rejects "$v" "got True"
v="$(variant zero-version)"; set_field "$v/grafana/manifest.json" template_version 0
check "template_version 0" rejects "$v" "got 0"
v="$(variant wrong-id)"; set_field "$v/grafana/manifest.json" id '"grafana2"'
check "id differs from the directory" rejects "$v" "\"id\" is 'grafana2', but the directory is 'grafana'"
v="$(variant no-template)"; rm "$v/uptime-kuma/docker-compose.yml.j2"
check "missing compose template" rejects "$v" "uptime-kuma: docker-compose.yml.j2 is missing"
v="$(variant symlink)"; ln -s /etc/passwd "$v/grafana/extra.txt"
check "symbolic link inside an app" rejects "$v" "grafana/extra.txt: symbolic links are not allowed"
v="$(variant schema)"; set_field "$v/grafana/manifest.json" name delete
check "schema violation" rejects "$v" "schema: (top level): 'name' is a required property"
v="$(variant stray)"; echo hi >"$v/README.txt"
check "stray file next to the apps" rejects "$v" "README.txt: unexpected entry"
v="$(variant bad-name)"; mv "$v/grafana" "$v/Grafana_App"
check "invalid app directory name" rejects "$v" "Grafana_App: directory name is not a valid app id"
v="$(variant no-schema)"; rm "$v/manifest.schema.json"
check "missing schema" rejects "$v" "manifest.schema.json: missing"
v="$(variant empty)"; rm -rf "$v/grafana" "$v/uptime-kuma"
check "no apps" rejects "$v" "no apps found"

# --- build two versions of the package --------------------------------------------
. "$ROOT/build/lib/common.sh"
nimbus_load_conf
build_version() {   # build_version VERSION CATALOG -> path of the .deb
	local dir="$WORK/build-$1/nimbus-catalog"
	mkdir -p "$dir"
	cp -a "$PKG/debian" "$dir/"
	cp -a "$2" "$dir/catalog"
	nimbus_render_tree "$dir"
	sed -i "1s/(0\.0\.0)/($1)/" "$dir/debian/changelog"
	(cd "$dir" && dpkg-buildpackage -b -us -uc) >"$WORK/build-$1.log" 2>&1 || { tail -n 30 "$WORK/build-$1.log" >&2; return 1; }
	printf '%s' "$WORK/build-$1/nimbus-catalog_$1_all.deb"
}
make_catalog "$WORK/v2" 2 "grafana/grafana:12.0.0"

echo "Package"
deb1="$(build_version 1.0 "$WORK/good")"
deb2="$(build_version 2.0 "$WORK/v2")"
check "builds (debian/rules ran check-catalog)" grep -q "check-catalog: 2 app(s) OK" "$WORK/build-1.0.log"
bad_build_fails() {
	! build_version 0.9 "$WORK/no-version" 2>/dev/null \
		&& grep -q 'check-catalog: .*"template_version" must be a whole number' "$WORK/build-0.9.log"
}
check "a catalog without template_version fails the build" bad_build_fails
check "lintian is clean" lintian --fail-on error,warning "$deb1"
check "no maintainer scripts at all" \
	sh -c '! dpkg-deb --ctrl-tarfile "$1" | tar -t | grep -Eq "(preinst|postinst|prerm|postrm|triggers)$"' _ "$deb1"
dpkg -i "$deb1" >/dev/null
C=/usr/share/nimbus-control/catalog
for f in "$C/manifest.schema.json" "$C/grafana/manifest.json" "$C/grafana/docker-compose.yml.j2" "$C/grafana/icon.svg" \
	"$C/uptime-kuma/manifest.json" /usr/share/doc/nimbus-catalog/SCHEMA.md; do
	check "installed: $f" test -s "$f"
done

# An installed stack of grafana, created from template_version 1, in a
# Control Center state directory that this test creates and removes again.
S=/var/lib/nimbus-control
[[ ! -e "$S" ]] || { echo "catalog.test: $S already exists; run this test in a fresh container" >&2; exit 2; }
mkdir -p "$S/stacks/grafana/data"
sed 's/{{ "\(.*\)" | q }}/"\1"/' "$C/grafana/docker-compose.yml.j2" >"$S/stacks/grafana/docker-compose.yml"
head -c 32768 /dev/urandom >"$S/stacks/grafana/data/grafana.db"
echo '{"stacks": {"grafana": {"app": "grafana", "template_version": 1}, "uptime-kuma": {"app": "uptime-kuma", "template_version": 1}}}' >"$S/stacks.json"
stack_before="$(cd "$S" && find stacks stacks.json -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)"
updates() {   # the stacks whose catalog app has a newer template_version
	python3 - "$S/stacks.json" "$C" <<'EOF'
import json, os, sys
stacks = json.load(open(sys.argv[1]))["stacks"]
out = []
for name, s in sorted(stacks.items()):
    m = json.load(open(os.path.join(sys.argv[2], s["app"], "manifest.json")))
    if m["template_version"] > s["template_version"]:
        out.append(name)
print(" ".join(out))
EOF
}
check "before the upgrade: no template update available" test -z "$(updates)"

echo "Upgrade (grafana template changed, template_version 2)"
dpkg -i "$deb2" >/dev/null
check "the installed manifest says template_version 2" grep -q '"template_version": 2' "$C/grafana/manifest.json"
check "the installed template is the new one" grep -q 'grafana/grafana:12.0.0' "$C/grafana/docker-compose.yml.j2"
check "the installed stack is byte for byte unchanged" \
	test "$(cd "$S" && find stacks stacks.json -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)" = "$stack_before"
check "Template update available: grafana, and only grafana" test "$(updates)" = grafana
dpkg --purge nimbus-catalog >/dev/null
check "purging the catalog leaves the stack alone" \
	test "$(cd "$S" && find stacks stacks.json -type f -print0 | LC_ALL=C sort -z | xargs -0 sha256sum)" = "$stack_before"
check "purging removes the catalog" test ! -e "$C"
rm -rf "$S"   # the simulated Control Center state, so other tests start clean

echo
printf 'nimbus-catalog catalog.test: %d passed, %d failed\n' "$passed" "$failed"
((failed == 0))
