<!--
  docs/TESTING.md: the QEMU smoke test.
  What: how to boot the NimbusOS ISO in QEMU (UEFI with KVM, q35 and OVMF;
        legacy BIOS), build a test ISO from a local test mirror, and the
        smoke-test checklist to run before a release.
  Why:  spec, deliverable 11; docs/PLAN.md NOTE-55, NOTE-63, NOTE-69,
        NOTE-72.
-->

# Testing NimbusOS

CI (`ci.yml`) tests every push: lint, unit tests, the seven packages with
lintian, and each package and integration test in a fresh Debian 13
container. A container can't boot an image, install it, format real disks,
or run a desktop. This smoke test does those in QEMU. Run it before every
stable release, and whenever the image, the installer or the Control
Center's storage, VM or update code changed.

It takes about two hours. Write the results into the release's pull
request or issue (see [Recording the result](#recording-the-result)).

## What you need

- **A Linux machine with KVM**, or Windows 11 with WSL 2 (Debian or Ubuntu).
  `ls -l /dev/kvm` must show the device, and your user must be in the `kvm`
  group.
- **Packages:** `sudo apt install qemu-system-x86 qemu-utils ovmf smbclient`.
- **Memory and disk:** 8 GB of free memory for a 6 GiB guest, and about
  60 GB of disk space. The disk images are sparse: they grow to roughly
  15 GB for the installed system plus what you copy to the data disks.
- **An ISO:** a test ISO built from a local test mirror (next section), or
  a release ISO. Steps 8 to 10 need the test mirror.
- **Nested virtualization** for the VM tab (step 4); see
  [Nested virtualization](#nested-virtualization).

## A test ISO from a local test mirror

`tools/test-mirror.sh` rehearses a release on your machine. It makes a
throwaway signing key, publishes packages into a local APT repository the way
`release.yml` does, and builds an ISO from them. Nothing is published, and
the real signing key isn't needed.

The packages and the ISO trust only the throwaway key, and they point at
`http://10.0.2.2:8080/apt`: the address under which a QEMU guest reaches the
host. They work only inside this test. The ISO is called
`nimbusos-test-amd64.iso`, and its volume label starts with
"NimbusOS TEST". Everything lives in `dist/test-mirror/`, which git ignores.

From the repository root, with everything committed (a release is built
from a clean tree, so the test refuses uncommitted changes):

```sh
tools/dev-shell.sh -- tools/test-mirror.sh init
tools/dev-shell.sh -- tools/test-mirror.sh publish v0.1.0
sudo tools/dev-shell.sh --privileged --work-volume nimbus-work -- tools/test-mirror.sh iso v0.1.0
```

- `init` creates the key and an empty signed repository.
- `publish v0.1.0` builds the seven packages as release `v0.1.0` and
  publishes them to the `stable` suite. The tag is only a label here; no
  git tag is made.
- `iso v0.1.0` builds the ISO from them, about an hour. It needs a
  privileged container run as root, because live-build creates device
  nodes. `--work-volume` keeps live-build's package cache, so a second
  build is much faster.

The ISO lands in `dist/test-mirror/iso/v0.1.0/`. Before you boot it, start
the mirror in a terminal of its own, and keep it running for the whole test:

```sh
tools/test-mirror.sh serve
```

It serves `dist/test-mirror/site` on `127.0.0.1:8080`, which the guest reaches
as `http://10.0.2.2:8080`.

A release ISO from GitHub works for steps 1 to 7, and steps 8 and 9 can be
read against the real repository. Step 10 needs the test mirror.

## Booting in QEMU

`tools/qemu-test.sh` runs the commands below and creates the disks and the
UEFI variable store on first use. `--dry-run` prints the command it would
run. The commands are written out here so you can run them by hand or
adapt them.

### UEFI with KVM, q35 and OVMF

```sh
mkdir -p vm-uefi && cd vm-uefi
qemu-img create -f qcow2 system.qcow2 64G
qemu-img create -f qcow2 data1.qcow2 20G
qemu-img create -f qcow2 data2.qcow2 20G
qemu-img create -f qcow2 parity1.qcow2 20G
# A writable copy of the UEFI variable store. OVMF keeps boot entries and
# the Secure Boot state in it; the template under /usr/share stays read-only.
cp /usr/share/OVMF/OVMF_VARS_4M.ms.fd OVMF_VARS_4M.ms.fd

qemu-system-x86_64 \
  -name nimbusos-test-uefi \
  -machine q35,accel=kvm,smm=on \
  -cpu host \
  -smp 4 \
  -m 6144 \
  -rtc base=utc \
  -global driver=cfi.pflash01,property=secure,value=on \
  -drive if=pflash,format=raw,unit=0,readonly=on,file=/usr/share/OVMF/OVMF_CODE_4M.secboot.fd \
  -drive if=pflash,format=raw,unit=1,file=OVMF_VARS_4M.ms.fd \
  -device virtio-vga \
  -device qemu-xhci \
  -device usb-tablet \
  -device virtio-rng-pci \
  -netdev user,id=net0,hostfwd=tcp::4445-:445 \
  -device virtio-net-pci,netdev=net0 \
  -drive file=system.qcow2,if=none,id=system,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=system,serial=NIMBUS-SYSTEM,bootindex=0 \
  -drive file=data1.qcow2,if=none,id=data1,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=data1,serial=NIMBUS-DATA1 \
  -drive file=data2.qcow2,if=none,id=data2,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=data2,serial=NIMBUS-DATA2 \
  -drive file=parity1.qcow2,if=none,id=parity1,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=parity1,serial=NIMBUS-PARITY1 \
  -drive file=../nimbusos-amd64.iso,if=none,id=cd,media=cdrom,readonly=on \
  -device ide-cd,drive=cd,bus=ide.0,bootindex=1
```

Or: `tools/qemu-test.sh --iso dist/test-mirror/iso/v0.1.0/nimbusos-test-amd64.iso`.

| Part | Why |
|---|---|
| `accel=kvm`, `-cpu host` | Full speed. The host's CPU model passes VT-x or AMD-V through, so the guest can run VMs itself (nested virtualization). |
| `smm=on`, `cfi.pflash01 … secure=on`, `OVMF_CODE_4M.secboot.fd`, `OVMF_VARS_4M.ms.fd` | UEFI with Secure Boot on and Microsoft's keys enrolled, like a typical PC. NimbusOS boots through Debian's signed shim and GRUB (PLAN NOTE-07). |
| `OVMF_VARS_4M.ms.fd` copy | The firmware writes boot entries into it. One copy per test machine; delete it to start over. |
| `system.qcow2` (64 GiB) | The installer needs 32 GB. |
| `data1`, `data2`, `parity1` (20 GiB each), with serial numbers | Two data disks and one parity disk for the NAS wizard, which asks you to type a disk's serial before it erases it. Parity must be at least as large as the largest data disk; equal sizes test that limit. |
| `bootindex=0` on the system disk, `bootindex=1` on the CD | The empty system disk falls through to the CD. After the install, the installed system starts first and the CD can stay attached. `tools/qemu-test.sh --boot-cd` boots the CD first. |
| `hostfwd=tcp::4445-:445` | The host reaches the guest's SMB server on port 4445. It listens on all of the host's addresses; write `tcp:127.0.0.1:4445-:445` to keep it on the host itself. |
| `virtio-vga`, `usb-tablet` | A display Plasma and the login screen drive without extra drivers, and a pointer that follows the host's mouse exactly. |

**Without Secure Boot:** drop `smm=on` and the `-global` line, and use
`OVMF_CODE_4M.fd` with a copy of `OVMF_VARS_4M.fd` (`tools/qemu-test.sh
--no-secure-boot`).

### Legacy BIOS

The same machine without the UEFI firmware lines: QEMU's SeaBIOS starts
the ISO's ISOLINUX menu, and the installer sets up GRUB for BIOS. Use its own
disks, because a system installed in UEFI mode doesn't start in BIOS mode.

```sh
mkdir -p vm-bios && cd vm-bios
qemu-img create -f qcow2 system.qcow2 64G
qemu-img create -f qcow2 data1.qcow2 20G
qemu-img create -f qcow2 data2.qcow2 20G
qemu-img create -f qcow2 parity1.qcow2 20G

qemu-system-x86_64 \
  -name nimbusos-test-bios \
  -machine q35,accel=kvm \
  -cpu host \
  -smp 4 \
  -m 6144 \
  -rtc base=utc \
  -device virtio-vga \
  -device qemu-xhci \
  -device usb-tablet \
  -device virtio-rng-pci \
  -netdev user,id=net0,hostfwd=tcp::4445-:445 \
  -device virtio-net-pci,netdev=net0 \
  -drive file=system.qcow2,if=none,id=system,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=system,serial=NIMBUS-SYSTEM,bootindex=0 \
  -drive file=data1.qcow2,if=none,id=data1,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=data1,serial=NIMBUS-DATA1 \
  -drive file=data2.qcow2,if=none,id=data2,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=data2,serial=NIMBUS-DATA2 \
  -drive file=parity1.qcow2,if=none,id=parity1,format=qcow2,discard=unmap \
  -device virtio-blk-pci,drive=parity1,serial=NIMBUS-PARITY1 \
  -drive file=../nimbusos-amd64.iso,if=none,id=cd,media=cdrom,readonly=on \
  -device ide-cd,drive=cd,bus=ide.0,bootindex=1
```

Or: `tools/qemu-test.sh --bios --iso <ISO>`.

### Nested virtualization

Step 4 starts a VM inside the NimbusOS guest. That needs nested
virtualization on the host, and `-cpu host` (both commands above have it).

Check it on the host:

```sh
cat /sys/module/kvm_intel/parameters/nested    # Intel: Y
cat /sys/module/kvm_amd/parameters/nested      # AMD: 1
```

Turn it on, with no VMs running (on AMD, `kvm_amd` instead of `kvm_intel`):

```sh
echo "options kvm_intel nested=1" | sudo tee /etc/modprobe.d/kvm-nested.conf
sudo modprobe -r kvm_intel && sudo modprobe kvm_intel
```

WSL 2 on Windows 11 has it on by default (`nestedVirtualization=true` in
`.wslconfig`). In the guest, `ls -l /dev/kvm` shows whether it arrived.
`tools/qemu-test.sh` warns when it is off.

## The smoke test

The steps are in the order that works: Jellyfin binds to the `media` share
only if the share exists when it is installed, and the Dashboard's numbers
need a running app and VM to compare. Each step names the spec's step
number. Commands marked **guest** run in a terminal (Konsole) in the
NimbusOS guest; **host** runs on your machine, from the repository root.

The live user is `nimbus`, password `live`. For the installed system,
create a user `tester` in the installer.

### 1. Live boot, install, reboot (spec step 1)

Boot the UEFI machine.

1. The boot menu says **NimbusOS** and offers "Start NimbusOS", "Start
   NimbusOS (safe graphics)" and "Start NimbusOS (fail-safe mode)", with a
   10-second countdown.
2. The NimbusOS splash appears, then the Plasma desktop, logged in as
   `nimbus` without a password. **guest:** `echo $XDG_SESSION_TYPE` prints
   `wayland` (PLAN NOTE-63).
3. The desktop has **Install NimbusOS**. In the installer:
   - the welcome page's checks pass (at least 32 GB of disk, 4 GB of memory);
   - Partitions: **Erase disk** on the 64 GiB disk. The three 20 GiB disks
     stay empty;
   - Users: name `tester`, a password, and the computer name `nimbusos`.
   Install, then **Restart now**.
4. The installed system starts from the disk (the CD stays attached) into
   the login screen. Log in as `tester`.

Check in the installed system (**guest**):

```sh
grep -E '^(NAME|VERSION_ID|NIMBUSOS_CHANNEL)=' /etc/os-release
sudo dmesg | grep -i 'secure boot'
ls /boot/efi/EFI
df -h /
systemctl --user show-environment | grep XDG_CONFIG_DIRS
id
ls /usr/lib/nimbusos-live
```

- `NAME="NimbusOS"`, `VERSION_ID` the ISO's version, `NIMBUSOS_CHANNEL=stable`.
- `Secure boot enabled` (UEFI with Secure Boot; PLAN NOTE-07, A6).
- `EFI/` holds `nimbusos` and `BOOT`.
- Used space on `/` is well under the installer's 32 GB minimum (PLAN
  NOTE-63). Write the figure down.
- `XDG_CONFIG_DIRS=/home/tester/.config/kdedefaults:/etc/xdg/nimbusos:/etc/xdg`: Plasma puts the user's `kdedefaults` first by itself, and `/etc/xdg/nimbusos` comes before `/etc/xdg` (PLAN NOTE-55).
- `tester` is in `docker`, `libvirt`, `kvm` and `nimbus-control`.
- `/usr/lib/nimbusos-live` doesn't exist: the installer removed the
  live-only files.

Open **System Settings → About this System** (or run `kinfocenter`): it
shows NimbusOS with its logo and website (PLAN NOTE-55).

Repeat steps 1 to 4 on the BIOS machine. Its boot menu is ISOLINUX's, with
the same three entries.

### 2. The NAS wizard in Pooled + parity mode (spec step 6)

Open **Nimbus Control Center** from the panel or the menu. It opens on the
Dashboard. Go to **Storage & Shares** and choose **Set up NAS**.

1. Disks: `NIMBUS-DATA1` and `NIMBUS-DATA2` as data, `NIMBUS-PARITY1` as
   parity. The system disk isn't offered.
2. Mode: **Pooled + parity**.
3. Shares: keep `media` (and add others if you like).
4. First user: `tester`, with a password for SMB.
5. Confirm: type each disk's serial number, then start. The log shows the
   disks being formatted and mounted.

Check (**guest**):

```sh
lsblk -o NAME,SERIAL,SIZE,FSTYPE,LABEL,MOUNTPOINTS
findmnt /mnt/user
sed -n '/# BEGIN nimbus/,/# END nimbus/p' /etc/fstab
sudo snapraid -c /etc/nimbus/snapraid.conf status
testparm -s 2>/dev/null | sed -n '/^\[media\]/,/^$/p'
```

- Both data disks and the parity disk are XFS, mounted under `/mnt/disks`
  and `/mnt/parity`; `/mnt/user` is a `fuse.mergerfs` pool over the two
  data disks.
- The fstab block mounts them by `UUID=` with `nofail`.
- SnapRAID knows both data disks and the parity disk.
- `[media]` has `force group = nimbus-smb`.

Restart the guest once: the pool and the shares come back.

### 3. SMB from the host (spec step 7)

**host:**

```sh
printf 'hello from the host\n' > to-guest.txt
smbclient -p 4445 //127.0.0.1/media -U tester -c 'put to-guest.txt; ls; get to-guest.txt back.txt'
cmp to-guest.txt back.txt && echo "same file both ways"
```

Then copy a file from the guest into the share (for example with
Dolphin at `/mnt/user/media`, or `cp`) and fetch it on the host with
`smbclient … -c 'get <name>'`.

### 4. Jellyfin bound to the media share (spec step 3)

**Apps → Catalog → Jellyfin → Install.** With the `media` share in place,
the one-click install binds Jellyfin's `/media` to the share's folder.

- The install log ends with Jellyfin running.
- The Dashboard's **Quick open** shows a Jellyfin tile (`nimbusos:8096`);
  it opens Jellyfin's setup page.
- **guest:**
  `docker inspect jellyfin-jellyfin-1 --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'`
  lists the media share's folder for `/media`.

### 5. A VM and its console from the Dashboard (spec step 4)

Needs nested virtualization. **Virtual machines → Create VM**: a Linux VM with
1 vCPU and 1 GiB of memory, booting an installer ISO (upload one, or copy it
into the `isos` share). Start it.

- The Dashboard's Virtual machines row shows it running, with **Console**.
  **Console** (or the VM's tile under Quick open) opens its screen in the
  page, and the keyboard reaches the VM.

### 6. The Dashboard's numbers (spec step 2)

With Jellyfin and the VM running, compare the Dashboard with Docker's and
libvirt's own numbers (**guest**):

```sh
nproc
docker stats --no-stream --format 'table {{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}'
sudo virsh domstats --cpu-total --balloon --vcpu
```

- **Docker apps.** The Dashboard shows CPU as a share of the whole machine;
  `docker stats` counts 100 % per CPU. Divide `docker stats`' CPU by
  `nproc` to compare. Memory is the same measure in both: use minus
  reclaimable page cache. They agree within a few percent.
- **VMs.** The Dashboard's CPU is the VM's use of its own vCPUs; memory is
  its resident memory, `balloon.rss` (in KiB). For CPU, take `cpu.time`
  (nanoseconds) twice, 10 seconds apart:
  `(cpu.time₂ − cpu.time₁) / (10 × 10⁹ × vcpu.current) × 100`.
- The exact values the Dashboard draws:
  `curl -s -H "Authorization: Bearer $(cat /run/nimbus-control/token)" http://127.0.0.1:7777/api/dashboard | python3 -m json.tool`
  (`apps` and `vms`).

### 7. `docker kill` raises an alert within 4 seconds (spec step 5)

Keep the Dashboard visible. **guest:**

```sh
date +%T.%N; docker kill jellyfin-jellyfin-1
```

"Needs attention" shows that Jellyfin exited unexpectedly within 4 seconds
of the time printed. Its **Start** button brings it back, and the alert
clears.

### 8. SnapRAID sees a move (PLAN NOTE-69)

On the **Pool** tab, **Sync now** and wait for it to finish. Then **guest:**

```sh
head -c 50M /dev/urandom > /mnt/user/media/moved.bin
sudo snapraid -c /etc/nimbus/snapraid.conf sync
mv /mnt/user/media/moved.bin /mnt/user/media/renamed.bin
sudo snapraid -c /etc/nimbus/snapraid.conf diff
```

- `diff` lists `move media/moved.bin -> media/renamed.bin`. (A container
  can't produce this line; real disks with UUIDs do.)
- The parity card shows about 50 MB not yet protected, which includes the
  moved file. **Sync now** brings it back to nothing.

### 9. Updating from v0.1.0 to v0.1.1-beta.1 (spec step 8)

**host:** publish the beta to the test mirror:

```sh
tools/dev-shell.sh -- tools/test-mirror.sh publish v0.1.1-beta.1
```

In the guest, **Updates**:

1. **Check now** on Stable: nothing new from NimbusOS.
2. Channel **Beta** (confirm with **Switch to Beta**), then **Check now**: the NimbusOS
   group lists the seven packages at `0.1.1~beta.1`, and the Dashboard shows
   "NimbusOS 0.1.1-beta.1 is available".
3. **Install updates.** The log streams into the drawer. The Control Center
   restarts during the update and reconnects by itself.

Then:

- the sidebar shows `0.1.1-beta.1`, and **guest:** `grep VERSION /etc/os-release`
  agrees;
- the SMB copy from step 3 still works, and Jellyfin's tile still opens it;
- the Dashboard's update alert is gone.

### 10. `apt-cache policy`: the repository can't replace a Debian package (spec step 9)

**host:** plant a fake `bash` 99.0 in the test mirror, as a compromised
repository might:

```sh
tools/dev-shell.sh -- tools/test-mirror.sh plant bash 99.0
```

**guest:**

```sh
sudo apt update
apt-cache policy bash nimbus-control
```

- `bash`: **Candidate** is Debian's installed version. The mirror's
  `99.0` is listed with priority **1**.
- `nimbus-control`: the mirror's versions have priority **500**.

**host:** `tools/dev-shell.sh -- tools/test-mirror.sh unplant bash`.

On a release install, `apt-cache policy` shows the same priorities for the
real repository: 1 for everything from it, 500 for `nimbus-*` and
`nimbusos-*`.

### 11. A broken signature is an error (spec step 10)

**host:** `tools/test-mirror.sh break-signature` (add `break-signature beta`
while the guest is on Beta).

In the guest, **Updates → Check now**:

- a red banner says the NimbusOS repository's signature couldn't be
  verified, with what to do. It never says "no updates";
- the Dashboard shows "The NimbusOS update repository can't be checked".

**host:** `tools/test-mirror.sh repair`, then **Check now** again: the
banner and the alert are gone.

## Recording the result

Copy this into the release's pull request or issue, and fill it in:

```text
NimbusOS smoke test: <version>, <ISO file name and SHA-256>
Host: <OS, CPU>, QEMU <version>, nested virtualization <on/off>
UEFI + Secure Boot  1 live/install/reboot [ ]  used space on / ____ GB
BIOS                1 live/install/reboot [ ]
2 NAS wizard, Pooled + parity [ ]   3 SMB both ways [ ]
4 Jellyfin on media, Open tile [ ]  5 VM console from the Dashboard [ ]
6 Dashboard vs docker stats / virsh domstats [ ]  7 alert within 4 s [ ]
8 SnapRAID move line [ ]           9 update to the beta [ ]
10 apt-cache policy [ ]            11 broken signature [ ]
Notes:
```

## When something fails

| Symptom | Check |
|---|---|
| `/dev/kvm is missing or not usable` | Virtualization (VT-x or AMD-V) on in the firmware settings; your user in the `kvm` group. |
| The guest can't reach the mirror | `tools/test-mirror.sh serve` is running; on the host, `curl -sI http://127.0.0.1:8080/apt/dists/stable/InRelease` answers 200. |
| The installed system doesn't start with Secure Boot | PLAN NOTE-07 and assumption A6: report it with a photo of the screen. Try `--no-secure-boot` to see whether only Secure Boot fails. |
| QEMU stops with `KVM: entry failed, hardware error 0xffffffff` and `SMM=1` (WSL 2) | WSL 2's KVM runs inside Hyper-V, which can't run the System Management Mode that OVMF's Secure Boot firmware uses once the kernel calls it. The boot menu still appears. Use `--no-secure-boot` there, and test Secure Boot on a Linux host or a real PC. |
| `mv: … Cannot allocate memory` at the end of the ISO build (WSL 2) | Writing a large file to the Windows drive (`/mnt/c`) through WSL can fail under memory pressure, and `dist/` is in the checkout. Clone the repository into WSL's own file system (for example `~/NimbusOS`) and build there. The finished ISO is still in the work volume as `nimbusos-amd64.hybrid.iso` (`sudo podman volume inspect nimbus-work` shows where). |
| WSL stops answering while the guest runs | WSL takes at most half of Windows' memory by default. Close other programs, give the guest less (`--memory 4608`), or raise WSL's limit in `.wslconfig`. |
| The VM tab can't start a VM | Nested virtualization: `ls -l /dev/kvm` in the guest. |
| `test-mirror.sh publish` refuses | Commit your changes first; the test builds from a clean tree, like a release. |
| `test-mirror.sh iso` says the address is its own gateway | Rootless Podman uses `10.0.2.2` itself. Run the build as root (`sudo`), as shown above. |
