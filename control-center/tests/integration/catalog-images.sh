#!/bin/bash
# =============================================================================
# control-center/tests/integration/catalog-images.sh
# =============================================================================
# What: checks the real app catalog (catalog/) against the outside world, in
#       a throwaway container, from the source tree:
#         1. every image a template names exists on its registry with that
#            tag, for linux/amd64 (skopeo, no Docker daemon), and prints its
#            version label and build date;
#         2. Docker's own CLI and Compose plugin (docker-ce-cli and
#            docker-compose-plugin from Docker's repository, signed with the
#            key in build/config/archives/) run the pytest checks that need
#            them: "docker compose config" accepts every app's 1-click
#            Compose file (test_catalog.py) and the engine's output for the
#            fixture apps (test_compose.py).
#       Starting the apps needs a Docker daemon with cgroups, which a
#       rootless container can't give; deliverable 11's QEMU test installs
#       Jellyfin from the catalog on a real NimbusOS.
# Why:  docs/PLAN.md NOTE-70: "verify, don't invent" for image names and tags,
#       which change outside this repository.
#
# Usage: control-center/tests/integration/catalog-images.sh
#        Root, in a throwaway container only (tools/dev-shell.sh). Network
#        (deb.debian.org, download.docker.com, Docker Hub and ghcr.io).
# =============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CC="$(dirname "$(dirname "$HERE")")"
REPO="$(dirname "$CC")"
[[ "$(id -u)" -eq 0 ]] || { echo "catalog-images: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] || { echo "catalog-images: throwaway container only" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends gpg ca-certificates skopeo jq python3 python3-pytest python3-fastapi \
	python3-httpx python3-psutil python3-websockets python3-jinja2 python3-yaml python3-jsonschema >/tmp/ci-install.log 2>&1 \
	|| { tail -n 20 /tmp/ci-install.log; exit 1; }
gpg --dearmor <"$REPO/build/config/archives/docker.key.chroot" >/usr/share/keyrings/docker-test.gpg
echo "deb [signed-by=/usr/share/keyrings/docker-test.gpg] https://download.docker.com/linux/debian trixie stable" \
	>/etc/apt/sources.list.d/docker-test.list
apt-get update -q >/dev/null 2>&1
apt-get install -y -q --no-install-recommends docker-ce-cli docker-compose-plugin >>/tmp/ci-install.log 2>&1 \
	|| { tail -n 20 /tmp/ci-install.log; exit 1; }
echo "$(skopeo --version); $(docker --version); $(docker compose version)"

passed=0
failed=0
echo "Images (linux/amd64)"
# Every service's image in every template, rendered with stand-in values.
mapfile -t images < <(python3 - "$REPO/catalog" <<'PY'
import pathlib, sys, yaml
from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
env = SandboxedEnvironment(undefined=StrictUndefined)
env.filters["q"] = lambda v: '"x"'
for tpl in sorted(pathlib.Path(sys.argv[1]).glob("*/docker-compose.yml.j2")):
    doc = yaml.safe_load(env.from_string(tpl.read_text()).render(
        stack="x", env={}, ports={}, host_ip="", hostname="", timezone="", share_gid=0))
    for service in doc["services"].values():
        print(f"{tpl.parent.name} {service['image']}")
PY
)
for line in "${images[@]}"; do
	app="${line%% *}"
	image="${line#* }"
	if info="$(skopeo inspect --retry-times 3 --override-os linux --override-arch amd64 "docker://$image" 2>&1)"; then
		passed=$((passed + 1))
		printf '  PASS  %-20s %s  (%s, built %s)\n' "$app" "$image" \
			"$(jq -r '.Labels["org.opencontainers.image.version"] // "no version label"' <<<"$info")" \
			"$(jq -r '.Created[:10]' <<<"$info")"
	else
		failed=$((failed + 1))
		printf '  FAIL  %-20s %s: %s\n' "$app" "$image" "$(tail -n 1 <<<"$info")"
	fi
done

echo "docker compose config"
cd "$CC"
if python3 -m pytest -q -p no:cacheprovider -k "real_docker_compose" tests/test_catalog.py tests/test_compose.py \
		>/tmp/ci-pytest.log 2>&1; then
	passed=$((passed + 1))
	echo "  PASS  $(tail -n 1 /tmp/ci-pytest.log)"
else
	failed=$((failed + 1))
	echo "  FAIL  pytest:"; tail -n 30 /tmp/ci-pytest.log
fi
grep -q " skipped" /tmp/ci-pytest.log && { failed=$((failed + 1)); echo "  FAIL  some checks were skipped: Docker's CLI wasn't found"; }

echo
echo "catalog-images: $passed passed, $failed failed"
((failed == 0))
