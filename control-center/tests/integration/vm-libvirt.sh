#!/bin/bash
# =============================================================================
# control-center/tests/integration/vm-libvirt.sh
# =============================================================================
# What: runs the VM code against a real libvirtd in a throwaway container:
#         1. installs Trixie's libvirt, QEMU, OVMF, swtpm, websockify, noVNC
#            and the Control Center's Python dependencies;
#         2. starts virtlogd and libvirtd by hand (no systemd in a container)
#            and the "default" NAT network. Container-only settings in
#            qemu.conf: QEMU runs as root without cgroups, AppArmor or
#            ownership changes, which a container can't provide; none of
#            this is how NimbusOS runs;
#         3. NIMBUS_TEST_LIBVIRT=1 pytest test_vm_libvirt.py: real VMs (KVM
#            if /dev/kvm is usable, else TCG), Secure Boot firmware, TPM,
#            websockify, delete with disk;
#         4. with OUT_DIR: ui_preview.py --live-vm takes screenshots of the
#            VMs page and of a real VM's console open in the page.
# Why:  docs/PLAN.md NOTE-67: the fakes in test_vms.py are checked against
#       the real thing.
#
# Usage: [NIMBUS_TEST_TCG=1] control-center/tests/integration/vm-libvirt.sh [OUT_DIR]
#        Root, in a privileged throwaway container only
#        (tools/dev-shell.sh --privileged). Network (deb.debian.org).
# =============================================================================
set -Eeuo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTS="$(dirname "$HERE")"
OUT="${1:-}"
[[ "$(id -u)" -eq 0 ]] || { echo "vm-libvirt: run as root in a throwaway container" >&2; exit 2; }
[[ -f /.dockerenv || -f /run/.containerenv ]] || { echo "vm-libvirt: throwaway container only" >&2; exit 2; }
export DEBIAN_FRONTEND=noninteractive

apt-get update -q >/dev/null
apt-get install -y -q --no-install-recommends \
	libvirt-daemon-system libvirt-daemon-driver-qemu libvirt-daemon-config-network libvirt-clients \
	qemu-system-x86 qemu-utils ovmf swtpm-tools dnsmasq-base iptables iproute2 websockify novnc \
	python3-libvirt python3-pytest python3-fastapi python3-uvicorn python3-httpx python3-psutil python3-websockets \
	python3-jinja2 python3-yaml python3-jsonschema >/tmp/vm-libvirt-install.log 2>&1 \
	|| { tail -n 30 /tmp/vm-libvirt-install.log; exit 1; }
if [[ -n "$OUT" ]]; then
	apt-get install -y -q --no-install-recommends chromium fonts-inter fonts-jetbrains-mono >>/tmp/vm-libvirt-install.log 2>&1
fi

# Container-only: no xattrs, cgroups, AppArmor or chown for QEMU here.
cat >>/etc/libvirt/qemu.conf <<'EOF'
user = "root"
group = "root"
dynamic_ownership = 0
remember_owner = 0
security_driver = "none"
cgroup_controllers = [ ]
EOF
# NIMBUS_TEST_TCG=1 (exported to test_vm_libvirt.py): the VMs use QEMU's
# emulator even where KVM works. ci.yml sets it: on GitHub's runners KVM is
# nested inside their virtual machine, and QEMU processes there got stuck so
# that SIGKILL couldn't end them ("Failed to terminate process … Device or
# resource busy", docs/PLAN.md NOTE-71).
export NIMBUS_TEST_TCG="${NIMBUS_TEST_TCG:-}"
mkdir -p /run/libvirt /var/lib/nimbus-vms/isos /var/lib/nimbus-vms/images
/usr/sbin/virtlogd -d
/usr/sbin/libvirtd -d
for _ in $(seq 50); do [[ -S /run/libvirt/libvirt-sock ]] && break; sleep 0.2; done
virsh -c qemu:///system net-start default >/dev/null
echo "libvirt $(virsh -c qemu:///system version --daemon | sed -n 's/^Running against daemon: //p'), $(qemu-system-x86_64 --version | head -1)"
if virsh -c qemu:///system domcapabilities --virttype kvm >/dev/null 2>&1; then echo "KVM: yes"; else echo "KVM: no (TCG)"; fi

NIMBUS_TEST_LIBVIRT=1 python3 -m pytest -q -p no:cacheprovider "$TESTS/test_vm_libvirt.py"
if [[ -n "$OUT" ]]; then
	python3 "$TESTS/ui_preview.py" --live-vm "$OUT"
fi
