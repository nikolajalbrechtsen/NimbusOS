<!--
  README.md: the project page on GitHub.
  What: what NimbusOS is; how to download, verify and install it; how
        updates work; what to know about security; how to build it.
  Why:  spec 4.5 (README.md) and docs/PLAN.md NOTE-46.
-->

# NimbusOS

NimbusOS is a desktop and home-server operating system built on **Debian 13
"Trixie"** with **KDE Plasma 6**. It adds the **Nimbus Control Center**, one
window for what a home server does:

- **Dashboard:** live CPU, memory, network and storage, what uses them (the
  desktop, each Docker app, each virtual machine), and a list of anything
  that needs your attention.
- **Apps:** ten Docker apps in one click (Portainer, Uptime Kuma,
  Vaultwarden, Nextcloud All-in-One, Jellyfin, Pi-hole, Syncthing, Home
  Assistant, Grafana, Nginx Proxy Manager), or set up exactly as you want.
- **Virtual machines:** Linux and Windows VMs with KVM/QEMU, in the window.
- **Storage & Shares:** turn spare disks into a pool (with parity if you
  like) and share folders with Windows, macOS and Linux over SMB.
- **Updates:** Debian's security updates every day, NimbusOS updates from
  this project, with release notes and a choice of Stable or Beta.

Everything else is Debian: the same packages, the same security updates.

## Download

Get the ISO from the [Releases](https://github.com/nikolajalbrechtsen/NimbusOS/releases)
page. Each release lists its files and the exact commands to check them.

An ISO larger than 2 GiB (GitHub's limit for one file) comes in parts,
`nimbusos-amd64.iso.part00`, `.part01`, and so on. Download them all and join
them, as the release page shows: `cat nimbusos-amd64.iso.part* > nimbusos-amd64.iso`
on Linux or macOS, or the short PowerShell loop given there on Windows.

### Check the download

Every release has `SHA256SUMS` and its signature `SHA256SUMS.gpg`, made with
the NimbusOS archive key: the same key that signs the update repository.

```sh
curl -fsSLO https://nikolajalbrechtsen.github.io/NimbusOS/apt/nimbusos-archive-keyring.asc
gpg --show-keys nimbusos-archive-keyring.asc        # compare with the fingerprint on the release page
gpg --import nimbusos-archive-keyring.asc
gpg --verify SHA256SUMS.gpg SHA256SUMS               # must say "Good signature"
grep ' nimbusos-amd64.iso$' SHA256SUMS | sha256sum -c   # macOS: shasum -a 256 -c
```

The public key is also in this repository, in
[`packages/nimbusos-archive-keyring/keys/`](packages/nimbusos-archive-keyring/keys/).

## Install

You need a 64-bit PC (amd64) with UEFI or legacy BIOS, at least 4 GB of
memory and 32 GB of disk. For virtual machines, the CPU's virtualization
must be switched on in the firmware settings (Intel VT-x or AMD-V).

1. Write the ISO to a USB stick of 8 GB or more. On Linux:
   `sudo dd if=nimbusos-amd64.iso of=/dev/sdX bs=4M status=progress oflag=sync`
   (replace `/dev/sdX` with the stick; everything on it is erased). On
   Windows, use [Rufus](https://rufus.ie/) and choose "DD image mode" when it
   asks.
2. Start the computer from the stick. NimbusOS starts as a live session,
   so you can try it without changing the disk.
3. Open **Install NimbusOS** on the desktop and follow the installer
   (Calamares): language, keyboard, disk, and your user.
4. Restart and remove the stick. The Nimbus Control Center is in the panel
   and the app menu.

To use the computer as a NAS, open **Storage & Shares** and choose **Set up
NAS**: pick the disks, a mode, the first user and the shares.

## Updates

- **Debian security updates** install every day by themselves.
- **NimbusOS updates** come from this project's signed APT repository,
  `https://nikolajalbrechtsen.github.io/NimbusOS/apt/`. In the Control
  Center, under **Updates**, choose **Automatic** (the default), **Notify
  only** or **Off**, and the **Stable** or **Beta** channel.
- **Other Debian updates** arrive with **Install updates**, or through
  Discover.
- NimbusOS **never restarts by itself**. When an update needs a restart, the
  Control Center says so.

The NimbusOS repository is trusted only for NimbusOS's own packages
(`nimbus-*` and `nimbusos-*`). Everything else from it has priority 1, so
it can never replace a Debian package. You can check this with
`apt-cache policy`.

The repository keeps the last three versions of each package. To go back
to an earlier one: `sudo apt install nimbus-control=<version>` (the list is
in `apt-cache policy nimbus-control`).

From a terminal: `nimbus-update status`, `nimbus-update check`,
`nimbus-update install` and `nimbus-update channel stable|beta`
(`man nimbus-update`).

## Security

- **The Control Center is for the computer's administrator.** Users in the
  groups `docker` or `nimbus-control` can, in effect, act as root: Docker
  can mount any folder into a container. The installer adds the first user
  to these groups (and to `libvirt` and `kvm`). Don't add people you
  wouldn't give the root password to.
- The Control Center listens only on `127.0.0.1`, and a launch code of one
  use opens it. It is not reachable from the network.
- **NimbusOS ships without an active firewall.** If you switch one on,
  allow SMB for your shares, for example `sudo ufw allow 445/tcp`, or with
  firewalld `sudo firewall-cmd --permanent --add-service=samba` followed by
  `sudo firewall-cmd --reload`.
- Apps you install from the catalog publish their web pages on your
  network. Set their passwords on first use; each app's notes say how.

## Building NimbusOS

Everything that needs Debian runs in a throwaway `debian:trixie` container,
with Docker or Podman (on Windows, inside WSL):

```sh
tools/dev-shell.sh -- tools/build-debs.sh --install-build-deps --lintian --allow-test-key   # the seven packages
tools/dev-shell.sh --privileged -- build/build.sh                                         # the ISO, into dist/
tools/dev-shell.sh -- tools/lint.sh                                                       # shellcheck, ruff and more
```

A local ISO build needs about 40 GB of free disk and a published APT
repository (`build/build.sh --help`). Releases are built and published only
by GitHub Actions: [docs/RELEASING.md](docs/RELEASING.md) explains how, and
[docs/SIGNING.md](docs/SIGNING.md) how the signing key is made and kept.
The design, every decision and every deviation from the original
specification are in [docs/PLAN.md](docs/PLAN.md).

| Folder | What it holds |
|---|---|
| `packages/` | The seven Debian packages: release and keyrings, branding, the Control Center, the app catalog, and the `nimbusos-desktop` metapackage |
| `control-center/` | The Nimbus Control Center: backend (FastAPI), web interface, privileged helper, tests |
| `catalog/` | The app catalog ([catalog/SCHEMA.md](catalog/SCHEMA.md)) |
| `build/` | The live-build configuration and `build.sh` |
| `tools/` | Build, publishing and test tools |
| `.github/workflows/` | CI, releases, and the weekly repository refresh |

## License

The code and packaging are licensed under the GNU General Public License,
version 3 or later ([LICENSE](LICENSE)). The artwork (wallpaper, themes,
logo) is licensed under Creative Commons Attribution-ShareAlike 4.0
([LICENSES/CC-BY-SA-4.0.txt](LICENSES/CC-BY-SA-4.0.txt)). Each package's
`debian/copyright` says exactly which files are under which license. The
fonts, Inter and JetBrains Mono, come from Debian's packages under their own
license (SIL Open Font License).
