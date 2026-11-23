#!/bin/bash
# =============================================================================
# tools/qemu-test.sh: boot the NimbusOS ISO in QEMU for the smoke test
# =============================================================================
# What: runs the QEMU commands of docs/TESTING.md, so a test machine is the
#       same every time:
#         - KVM, q35, the host's CPU model (nested virtualization for the VM
#           tab), 4 vCPUs and 6 GiB of memory by default;
#         - UEFI with Secure Boot (OVMF with Microsoft's keys, like a typical
#           PC); --no-secure-boot for plain UEFI; --bios for legacy BIOS
#           (SeaBIOS, ISOLINUX on the ISO);
#         - a writable copy of the UEFI variable store, made once per machine,
#           so boot entries survive restarts;
#         - a 64 GiB system disk and three blank 20 GiB virtio disks, two for
#           data and one for parity, with serial numbers NIMBUS-DATA1,
#           NIMBUS-DATA2 and NIMBUS-PARITY1 (the NAS wizard asks for the
#           serial before it erases a disk);
#         - user networking with hostfwd=tcp::4445-:445, so the host reaches
#           the guest's SMB server at port 4445. The guest reaches the host's
#           loopback as 10.0.2.2 (tools/test-mirror.sh serve);
#         - the ISO as a CD. The system disk boots first and the CD second:
#           the empty disk falls through to the CD, and after the install the
#           installed system starts without removing the CD (--boot-cd turns
#           this around, for the live session on an installed machine).
#       UEFI and BIOS machines keep separate disks (DIR/uefi, DIR/bios): a
#       system installed in one mode doesn't boot in the other.
#
# Why:  spec, deliverable 11: the exact KVM/q35/OVMF command with the VARS
#       copy, three blank virtio disks and the SMB forward, and a second
#       command for legacy BIOS.
#
# Usage: tools/qemu-test.sh [options] [-- EXTRA QEMU ARGUMENTS]
#   --iso FILE          the ISO to attach as a CD (default: dist/nimbusos-amd64.iso;
#                       "none" for no CD)
#   --dir DIR           the machine's files (default: _build/qemu/uefi or
#                       _build/qemu/bios)
#   --bios              legacy BIOS instead of UEFI
#   --no-secure-boot    UEFI without Secure Boot
#   --boot-cd           boot the CD first
#   --memory MIB        guest memory in MiB (default 6144; the installer
#                       needs 4096)
#   --cpus N            virtual CPUs (default 4)
#   --system-size SIZE  system disk, created on first use (default 64G; the
#                       installer needs 32 GB)
#   --disk-size SIZE    each blank disk, created on first use (default 20G)
#   --smb-port PORT     host port forwarded to the guest's port 445 (default 4445)
#   --display SPEC      QEMU's -display, for example gtk, sdl or
#                       vnc=127.0.0.1:5 (default: QEMU's own choice)
#   --ovmf-code FILE    UEFI firmware (default: Debian's and Ubuntu's path)
#   --ovmf-vars FILE    UEFI variable store template, copied once (ditto)
#   --reset             delete the machine's disks and variable store first
#   --dry-run           print the QEMU command and change nothing
#   -h, --help          show this help
#   Needs qemu-system-x86, qemu-utils and ovmf (Debian package names), and
#   /dev/kvm. Runs as a normal user in the kvm group.
# =============================================================================
set -Eeuo pipefail

# shellcheck source=build/lib/common.sh
. "$(dirname "$(readlink -f "$0")")/../build/lib/common.sh"

usage() {
	sed -n '/^# Usage:/,/^# =====/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# The firmware files of Debian's and Ubuntu's ovmf package.
OVMF_DIR="/usr/share/OVMF"

iso="" dir="" firmware="uefi" secure_boot=1 boot_cd="" memory=6144 cpus=4
system_size="64G" disk_size="20G" smb_port=4445 display="" ovmf_code="" ovmf_vars=""
reset="" dry_run=""
extra=()

# The blank disks: file name and serial number (virtio-blk allows 20 characters).
DISKS=("data1:NIMBUS-DATA1" "data2:NIMBUS-DATA2" "parity1:NIMBUS-PARITY1")

# quote_arg ARG: ARG as the shell reads it back; quoted only when needed
# (printf %q would also escape every comma of QEMU's option lists).
quote_arg() {
	if [[ "$1" =~ ^[A-Za-z0-9_./:=,@+%-]+$ ]]; then
		printf '%s' "$1"
	else
		printf "'%s'" "${1//\'/\'\\\'\'}"
	fi
}

# print_command ARGS...: one option per line, ready to paste.
print_command() {
	local arg first=1
	for arg in "$@"; do
		if [[ -n "$first" ]]; then
			quote_arg "$arg"
			first=""
		elif [[ "$arg" == -* ]]; then
			printf ' \\\n  '
			quote_arg "$arg"
		else
			printf ' '
			quote_arg "$arg"
		fi
	done
	printf '\n'
}

int_between() {   # int_between VALUE MIN MAX: VALUE is a whole number in [MIN, MAX]
	[[ "$1" =~ ^[0-9]{1,9}$ ]] && ((10#$1 >= $2 && 10#$1 <= $3))
}

check_size() {   # check_size OPTION VALUE: a qemu-img size such as 64G
	[[ "$2" =~ ^[1-9][0-9]*[MGT]$ ]] || die "$1 must be a size such as 20G, got: $2"
}

nested_state() {   # prints on, off or unknown
	local f v
	for f in /sys/module/kvm_intel/parameters/nested /sys/module/kvm_amd/parameters/nested; do
		[[ -r "$f" ]] || continue
		v="$(<"$f")"
		case "$v" in
			Y|y|1) echo on ;;
			*) echo off ;;
		esac
		return 0
	done
	echo unknown
}

main() {
	local sys_index cd_index vars_src vars name serial machine d
	local -a qemu

	while (($#)); do
		case "$1" in
			--iso)            iso="${2:?--iso needs a file}"; shift 2 ;;
			--dir)            dir="${2:?--dir needs a directory}"; shift 2 ;;
			--bios)           firmware="bios"; shift ;;
			--no-secure-boot) secure_boot=""; shift ;;
			--boot-cd)        boot_cd=1; shift ;;
			--memory)         memory="${2:?--memory needs a number}"; shift 2 ;;
			--cpus)           cpus="${2:?--cpus needs a number}"; shift 2 ;;
			--system-size)    system_size="${2:?--system-size needs a size}"; shift 2 ;;
			--disk-size)      disk_size="${2:?--disk-size needs a size}"; shift 2 ;;
			--smb-port)       smb_port="${2:?--smb-port needs a number}"; shift 2 ;;
			--display)        display="${2:?--display needs a value}"; shift 2 ;;
			--ovmf-code)      ovmf_code="${2:?--ovmf-code needs a file}"; shift 2 ;;
			--ovmf-vars)      ovmf_vars="${2:?--ovmf-vars needs a file}"; shift 2 ;;
			--reset)          reset=1; shift ;;
			--dry-run)        dry_run=1; shift ;;
			-h|--help)        usage; exit 0 ;;
			--)               shift; extra=("$@"); break ;;
			*) die "Unknown option: $1 (see --help; QEMU's own options go after --)" ;;
		esac
	done
	int_between "$memory" 1024 4194304 || die "--memory must be a number of MiB, at least 1024."
	int_between "$cpus" 1 1024 || die "--cpus must be a positive number."
	int_between "$smb_port" 1 65535 || die "--smb-port must be a port number."
	check_size --system-size "$system_size"
	check_size --disk-size "$disk_size"
	if [[ "$firmware" == bios && -z "$secure_boot" ]]; then
		die "--no-secure-boot only applies to UEFI."
	fi
	if [[ "$firmware" == bios ]]; then
		secure_boot=""
	fi

	nimbus_load_conf
	iso="${iso:-$(nimbus_path "$NIMBUS_OUTPUT_DIR")/${NIMBUS_ISO_BASENAME}.iso}"
	if [[ "$iso" != none ]]; then
		iso="$(readlink -m -- "$iso")"
		[[ -f "$iso" || -n "$dry_run" ]] || die "$iso does not exist. Build it (build/build.sh, or tools/test-mirror.sh iso), download a release, or pass --iso."
	fi
	dir="$(readlink -m -- "${dir:-$NIMBUS_REPO_ROOT/_build/qemu/$firmware}")"

	if [[ "$firmware" == uefi ]]; then
		if [[ -n "$secure_boot" ]]; then
			ovmf_code="${ovmf_code:-$OVMF_DIR/OVMF_CODE_4M.secboot.fd}"
			vars_src="${ovmf_vars:-$OVMF_DIR/OVMF_VARS_4M.ms.fd}"
			vars="$dir/OVMF_VARS_4M.ms.fd"
		else
			ovmf_code="${ovmf_code:-$OVMF_DIR/OVMF_CODE_4M.fd}"
			vars_src="${ovmf_vars:-$OVMF_DIR/OVMF_VARS_4M.fd}"
			vars="$dir/OVMF_VARS_4M.fd"
		fi
		if [[ -z "$dry_run" ]]; then
			[[ -r "$ovmf_code" ]] || die "$ovmf_code not found. Install ovmf, or pass --ovmf-code."
			[[ -r "$vars_src" || -f "$vars" ]] || die "$vars_src not found. Install ovmf, or pass --ovmf-vars."
		fi
	fi

	# Boot order: system disk first, CD second (see What), unless --boot-cd.
	if [[ -n "$boot_cd" ]]; then
		cd_index=0 sys_index=1
	else
		sys_index=0 cd_index=1
	fi

	machine="q35,accel=kvm"
	if [[ -n "$secure_boot" ]]; then
		machine+=",smm=on"
	fi
	qemu=(qemu-system-x86_64
		-name "${NIMBUS_ID}-test-${firmware}"
		-machine "$machine"
		-cpu host
		-smp "$cpus"
		-m "$memory"
		-rtc base=utc)
	if [[ "$firmware" == uefi ]]; then
		if [[ -n "$secure_boot" ]]; then
			# Secure Boot firmware keeps its variables in SMM-protected flash.
			qemu+=(-global "driver=cfi.pflash01,property=secure,value=on")
		fi
		qemu+=(-drive "if=pflash,format=raw,unit=0,readonly=on,file=$ovmf_code"
			-drive "if=pflash,format=raw,unit=1,file=$vars")
	fi
	qemu+=(-device virtio-vga
		-device qemu-xhci -device usb-tablet
		-device virtio-rng-pci
		-netdev "user,id=net0,hostfwd=tcp::${smb_port}-:445"
		-device "virtio-net-pci,netdev=net0"
		-drive "file=$dir/system.qcow2,if=none,id=system,format=qcow2,discard=unmap"
		-device "virtio-blk-pci,drive=system,serial=NIMBUS-SYSTEM,bootindex=$sys_index")
	for d in "${DISKS[@]}"; do
		name="${d%%:*}" serial="${d#*:}"
		qemu+=(-drive "file=$dir/$name.qcow2,if=none,id=$name,format=qcow2,discard=unmap"
			-device "virtio-blk-pci,drive=$name,serial=$serial")
	done
	if [[ "$iso" != none ]]; then
		qemu+=(-drive "file=$iso,if=none,id=cd,media=cdrom,readonly=on"
			-device "ide-cd,drive=cd,bus=ide.0,bootindex=$cd_index")
	fi
	if [[ -n "$display" ]]; then
		qemu+=(-display "$display")
	fi
	qemu+=("${extra[@]}")

	if [[ -n "$dry_run" ]]; then
		print_command "${qemu[@]}"
		exit 0
	fi

	# --- checks and the machine's files ----------------------------------------
	command -v qemu-system-x86_64 >/dev/null 2>&1 || die "qemu-system-x86_64 not found. Install qemu-system-x86."
	command -v qemu-img >/dev/null 2>&1 || die "qemu-img not found. Install qemu-utils."
	[[ -r /dev/kvm && -w /dev/kvm ]] \
		|| die "/dev/kvm is missing or not usable. Turn on virtualization (VT-x or AMD-V) in the firmware settings and add yourself to the kvm group (sudo adduser \"\$USER\" kvm, then log in again). On Windows, run this in WSL 2."
	case "$(nested_state)" in
		on) ;;
		off) warn "Nested virtualization is off, so the VM tab can't start virtual machines inside this one (docs/TESTING.md, Nested virtualization)." ;;
		*) warn "Can't tell whether nested virtualization is on (no kvm_intel or kvm_amd module information)." ;;
	esac

	if [[ -n "$reset" ]]; then
		log "Resetting the machine in $dir"
		rm -f -- "$dir/system.qcow2" "$dir"/OVMF_VARS_4M*.fd
		for d in "${DISKS[@]}"; do
			rm -f -- "$dir/${d%%:*}.qcow2"
		done
	fi
	mkdir -p -- "$dir"
	if [[ ! -f "$dir/system.qcow2" ]]; then
		qemu-img create -q -f qcow2 "$dir/system.qcow2" "$system_size"
		log "Created the blank system disk $dir/system.qcow2 ($system_size)"
	fi
	for d in "${DISKS[@]}"; do
		name="${d%%:*}"
		if [[ ! -f "$dir/$name.qcow2" ]]; then
			qemu-img create -q -f qcow2 "$dir/$name.qcow2" "$disk_size"
			log "Created the blank disk $dir/$name.qcow2 ($disk_size, serial ${d#*:})"
		fi
	done
	if [[ "$firmware" == uefi && ! -f "$vars" ]]; then
		# The firmware writes its variables (boot entries, Secure Boot state)
		# into this copy; the template under /usr/share stays read-only.
		install -m 0644 -- "$vars_src" "$vars"
		log "Copied the UEFI variable store $vars_src to $vars"
	fi

	log "Starting ${NIMBUS_NAME} in QEMU ($firmware${secure_boot:+, Secure Boot}). SMB: smbclient -p $smb_port //127.0.0.1/<share> -U <user>"
	print_command "${qemu[@]}" >&2
	nimbus_exec "${qemu[@]}"
}

main "$@"
