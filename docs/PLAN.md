# NimbusOS — Build and Architecture Plan

This is deliverable 1 of 11. It covers the repository layout, how the parts connect, the assumptions the work rests on, and every place where the implementation differs from the original specification. Each difference is numbered `NOTE-nn`, and code comments in later deliverables refer back to these numbers.

| | |
|---|---|
| Base | Debian 13 "trixie", `amd64` only |
| Source | https://github.com/nikolajalbrechtsen/NimbusOS (public) |
| APT repository | https://nikolajalbrechtsen.github.io/NimbusOS/apt/ — suites `stable`, `beta`; component `main` |
| ISO | `nimbusos-amd64.iso` (iso-hybrid, UEFI + legacy BIOS) |

---

## 1. Repository tree

```
NimbusOS/
├── README.md                         What NimbusOS is, download + verify the ISO, how updates work (NOTE-46)
├── LICENSE                           GPL-3.0-or-later full text (code and packaging)
├── LICENSES/CC-BY-SA-4.0.txt         the artwork's license, SPDX's text at the commit debian/copyright pins (NOTE-71)
├── .gitattributes                    LF line endings everywhere (NOTE-50)
├── .gitignore                        build products, caches, anything that looks like a private key
├── .shellcheckrc                     repo-wide shellcheck settings (SC2016 off: deliberate `sh -c '…'`)
├── ruff.toml                         ruff's rule set (E, W, F, B, UP; Python 3.13), also for the programs without .py
│
├── .github/dependabot.yml            keeps the SHA-pinned GitHub Actions current (NOTE-42)
├── .github/workflows/
│   ├── ci.yml                        push + PR: lint, unit tests, all .debs with lintian, every package and integration test, Trixie/Docker checks
│   ├── release.yml                   tags vX.Y.Z / vX.Y.Z-beta.N: debs → APT → ISO → GitHub Release
│   └── refresh.yml                   weekly: re-sign Release files, bootstrap an empty repo once (NOTE-04); Docker key check
│
├── build/
│   ├── nimbus.conf                   every build tunable, one place (names, URLs, mirrors, live user, split size…)
│   ├── build.sh                      host checks → repo check → .debs → lb clean / lb config / lb build → ISO + .sha256
│   ├── lib/common.sh                 logging, nimbus.conf loading, version helpers (shared with tools/)
│   └── config/                       live-build config TEMPLATE; copied to build/work/ for each build
│       ├── package-lists/
│       │   ├── 10-base.list.chroot                  Depends (NOTE-48: no marker)
│       │   ├── 15-firmware.list.chroot              Recommends (#! nimbus: desktop-recommends)
│       │   ├── 20-plasma.list.chroot                Depends: Plasma core by name
│       │   ├── 25-desktop-extras.list.chroot        Recommends: apps, browsers, Discover, Flatpak, kde-plasma-desktop
│       │   ├── 30-theming-fonts.list.chroot         Depends
│       │   ├── 40-calamares.list.chroot             #! nimbus: exclude-from-desktop
│       │   ├── 50-docker.list.chroot                Depends
│       │   ├── 60-virtualization.list.chroot        Depends
│       │   ├── 70-storage-nas.list.chroot           Depends
│       │   ├── 75-storage-optional.list.chroot      Recommends: snapraid
│       │   ├── 90-nimbusos.list.chroot              nimbusos-desktop itself (#! nimbus: exclude-from-desktop)
│       │   ├── 95-installer-pool-bios.list.binary   grub-pc → ISO pool for Calamares' bootloader step
│       │   ├── 96-installer-pool-uefi.list.binary   grub-efi + signed GRUB/shim → ISO pool
│       │   └── 97-installer-pool-luks.list.binary   cryptsetup-initramfs, keyutils → ISO pool (encrypted installs)
│       ├── archives/                                Docker CE source (NOTE-03, NOTE-05)
│       │   ├── docker.list.chroot                   build-time source, trusted through docker.key.chroot (no signed-by)
│       │   ├── docker.key.chroot                    Docker's public key, byte-identical to Docker's download
│       │   └── docker.list.binary                   the image's source: signed-by=/usr/share/keyrings/docker-archive-keyring.gpg
│       │                                            (both lists state the fingerprint in a comment; CI checks it)
│       ├── hooks/live/                              run after Debian's own hooks (NOTE-62)
│       │   ├── 0100-flathub-preseed.hook.chroot     runs the packaged Flathub setup once at build time
│       │   ├── 0200-live-installer-launcher.hook.chroot  hides Debian's "Install Debian" menu entry and desktop icon
│       │   ├── 0300-live-units.hook.chroot          enables every live-only unit in includes.chroot_after_packages
│       │   ├── 0400-live-manifest.hook.chroot       lists the live-only files for nimbus-target-cleanup (NOTE-63)
│       │   └── 0900-verify-image.hook.chroot        fails the build if the image would be broken (18 checks)
│       ├── includes.chroot_after_packages/          live-session-only files (NOTE-02, NOTE-06, NOTE-63)
│       │   ├── etc/calamares/settings.conf          Debian's sequence, nimbusos branding, two NimbusOS steps
│       │   ├── etc/calamares/modules/{users,bootloader,welcome,packages,displaymanager}.conf
│       │   ├── etc/calamares/modules/shellprocess_nimbus-sources-final.conf
│       │   ├── etc/calamares/modules/shellprocess_nimbus-target-cleanup.conf
│       │   ├── etc/systemd/system/var-lib-{docker,containerd}.mount   tmpfs, live session only (NOTE-21)
│       │   ├── etc/xdg/autostart/nimbus-live-session.desktop
│       │   ├── usr/lib/nimbusos-live/{sources-final,target-cleanup,live-session-setup}
│       │   └── usr/share/applications/nimbus-install.desktop   "Install NimbusOS"
│       └── bootloaders/                  overlays; live-build copies them over its own templates itself (NOTE-64)
│           ├── splash.svg                one splash for both menus, rendered by live-build in the chroot (NOTE-12)
│           ├── grub-pc/{grub,theme}.cfg  GRUB (UEFI) menu: three "Start NimbusOS" entries; live-theme/ is filled from nimbusos-branding
│           ├── syslinux_common/          ISOLINUX (BIOS) menu: live.cfg.in entries, menu.cfg title, stdmenu.cfg colours
│           └── isolinux/isolinux.cfg     10-second timeout
│
├── packages/                         one Debian source package per directory: debhelper-compat (= 13), 3.0 (native)
│   ├── nimbusos-archive-keyring/     keys/*.asc → dearmored NimbusOS keyring
│   ├── nimbusos-docker-keyring/      Docker's key → /usr/share/keyrings/docker-archive-keyring.gpg (NOTE-05)
│   ├── nimbusos-release/             os-release, apt/*, nimbus-update/ (CLI, man page, default settings),
│   │                                 flathub/, systemd/*, tests/ (pytest, apt pinning proof, install test)
│   ├── nimbusos-branding/            art/*.svg, grub/, plymouth/, sddm/, calamares/, plasma/, color-schemes/, xdg/,
│   │                                 Makefile (renders PNGs and GRUB fonts), tests/install.test.sh
│   ├── nimbus-control/               debian/ and tests/; payload staged from control-center/ at build time
│   ├── nimbus-catalog/               debian/ and tests/; payload staged from catalog/ at build time
│   └── nimbusos-desktop/             metapackage; Depends generated from build/config/package-lists/
│
├── control-center/
│   ├── backend/nimbus_control/       FastAPI: app.py, auth.py, errors.py, config.py, fonts.py, privd_client.py,
│   │                                 system_service.py, updates_service.py, state.py, jobs.py; Apps: catalog.py,
│   │                                 manifest.py, compose.py, docker_api.py, apps_service.py; VMs: vm_service.py,
│   │                                 vm_xml.py, libvirt_api.py, iso_library.py; NAS: storage_service.py,
│   │                                 shares_service.py; Dashboard: metrics_service.py (collector), docker_stream.py
│   │                                 (Docker's streaming API), alerts.py ("Needs attention")
│   ├── common/nimbus_common/         shared by backend, privd and timers: validate.py, protocol.py (privd wire
│   │                                 format), shares.py (what a share is, where its folder is)
│   ├── privd/                        nimbus-privd.py + nimbus_privd/ (server, registry, runner, audit, updates,
│   │                                 system, stacks, storage (disks, pool, parity), samba (shares, SMB users),
│   │                                 fsutil (atomic files, the fstab block))
│   ├── frontend/                     index.html, console.html + console.js (pop-out VM console), style.css, app.js,
│   │                                 lib/{api,ui,icons,vnc,charts}.js,
│   │                                 views/{dashboard,apps,vms,storage,updates,settings}.js
│   ├── bin/                          nimbus-control-server, nimbus-control-open (launcher), nimbus-migrate,
│   │                                 nimbus-snapraid-run, nimbus-recycle-clean, nimbus-netfix, nimbus-grant-access
│   ├── migrations/                   NNNN_name.py: the state migration steps nimbus-migrate runs (NOTE-59)
│   ├── systemd/                      nimbus-control.service, nimbus-privd.{socket,service},
│   │                                 nimbus-websockify.service, nimbus-snapraid-sync.{service,timer},
│   │                                 nimbus-recycle-clean.{service,timer}, nimbus-netfix.service
│   ├── data/                         samba drop-in, polkit action, nimbus-control.desktop (icon: nimbusos-branding's logo)
│   └── tests/                        pytest (no network, no root; Docker/libvirt/privd faked); ui_preview.py (screenshots)
│       ├── fixtures/catalog/                         three demo apps for the Apps tests and screenshots
│       ├── integration/installed-smoke.sh            the installed package, run as its units would run it
│       ├── integration/vm-libvirt.sh                 a real libvirtd: VMs, firmware, TPM, websockify, live console shots
│       ├── integration/nas-integration.sh            real mergerfs, SnapRAID, Samba, smbclient and iptables
│       ├── integration/docker-streams.sh             a real dockerd: the events and stats streams the Dashboard reads
│       ├── integration/catalog-images.sh             the catalog's images on their registries; real "docker compose config"
│       └── integration/samba-ucf-keeps-include.sh   CI: a ucf-managed smb.conf update keeps the include (NOTE-23)
│
├── catalog/
│   ├── SCHEMA.md                     manifest schema, documented field by field; templates; changing an app
│   ├── manifest.schema.json          the same schema as JSON Schema (checked at package build and at runtime)
│   └── <app-id>/{manifest.json, docker-compose.yml.j2, icon.svg}
│        portainer, uptime-kuma, vaultwarden, nextcloud-aio, jellyfin,
│        pihole, syncthing, home-assistant, grafana, nginx-proxy-manager (NOTE-70)
│
├── tools/
│   ├── build-debs.sh                 stage + build all 7 .debs (used by build.sh, ci.yml, release.yml)
│   ├── deb-version.sh                git tag → Debian version (NOTE-32)
│   ├── gen-desktop-depends.sh        package lists → ${nimbus:ListDepends} substvar (NOTE-48)
│   ├── verify-trixie.sh              proves every listed package and live-build flag exists in trixie (§7)
│   ├── dev-shell.sh                  throwaway debian:trixie container (Docker or Podman); works in a clone by default
│   ├── check-file-modes.sh           git file modes must match tools/file-modes.conf (CI)
│   ├── file-modes.conf               which files must be executable, by role
│   ├── fetch-docker-key.sh           re-downloads Docker's key and refuses it unless the fingerprint matches
│   ├── lint.sh                       shellcheck, ruff, node --check, JSON/YAML, file modes, Docker key (ci.yml's lint job)
│   ├── import-signing-key.sh         loads the APT_SIGNING_KEY secret in a job; refuses a key that isn't committed
│   ├── pin-actions.sh                resolves each action's release tag to its commit SHA (NOTE-42)
│   ├── publish-apt.sh                apt-ftparchive repository on gh-pages: init, add (prune to 3), resign, check, reindex (NOTE-52)
│   ├── releases-json.py              maintains releases.json next to the repository (NOTE-37)
│   ├── split-iso.sh                  splits ISOs over the GitHub asset limit; writes join instructions
│   ├── qemu-test.sh                  the UEFI/BIOS QEMU test commands from docs/TESTING.md
│   ├── test-mirror.sh                a throwaway key and a local APT repository for the QEMU test: publish, iso, serve,
│   │                                 break-signature, plant (NOTE-72)
│   └── tests/
│       ├── deb-version.test.sh       version mapping (real dpkg; 40 checks)
│       ├── image-apt-check.test.sh   final-image APT check: good image + 6 failure modes
│       ├── docker-archive.test.sh    fetch-docker-key.sh --verify-local: real files + 8 breakages
│       ├── publish-apt.test.sh       pruning (dpkg order, ~beta), signing, re-signing, by-hash, with a real apt client
│       ├── split-iso.test.sh         ISO parts, SHA256SUMS, join instructions
│       ├── test-mirror.test.sh       the test mirror with a real apt client: broken and repaired signatures, a planted package
│       ├── hooks.test.sh             the live-build hooks in a container standing in for the chroot
│       ├── live-files.test.sh        the installer configuration and live-only scripts (sources, cleanup)
│       ├── bootloaders.test.sh       the live boot menus, made by live-build's own scripts
│       └── image-initrd-check.test.sh  the boot-splash check against real Trixie initramfs images
│
└── docs/
    ├── PLAN.md                       this file
    ├── RELEASING.md                  one-time GitHub setup, stable/beta releases, rollback
    ├── SIGNING.md                    offline ed25519 key, secrets, key rotation
    ├── TESTING.md                    QEMU commands, the test mirror, the smoke-test checklist
    └── release-notes/v0.1.0.md       release notes = GitHub Release body = releases.json notes (NOTE-37)
```

Rules for the whole tree:

- **`build/config/` is a template.** `build.sh` copies it to `build/work/config/`, adds the generated pieces there (filled-in `@NIMBUS_*@` tokens, the built `.deb` files, the GRUB theme), and runs live-build inside `build/work/`. A build never modifies the checked-in tree.
- **Anything that may change after install ships in a package.** `includes.chroot_after_packages/` holds only files used by the live session (Calamares configuration, live-only units and launchers). Calamares' last step deletes those files from the installed target.
- **No binary files are committed.** PNGs, GRUB `.pf2` fonts and binary keyrings are generated when the packages are built (NOTE-12).

---

## 2. Build and release flow

```
git tag -a v0.2.0  ──►  release.yml   (concurrency group "nimbusos-publish")
   │
   ├─ build-debs      container debian:trixie      tools/build-debs.sh → 7 × .deb ──► artifact "debs"
   ├─ publish-apt     container debian:trixie      tools/publish-apt.sh → gh-pages:/apt/{dists,pool}
   │                                               suite stable (or beta), signed, releases.json(.sig)
   ├─ build-iso       privileged debian:trixie     build/build.sh --debs-dir debs/ ──► artifact "iso"
   └─ github-release  ubuntu runner, gh CLI        SHA256SUMS + SHA256SUMS.gpg, split > 2 GiB, publish
```

**`build/build.sh`** (local use: `sudo ./build/build.sh`; or `--from-repo [--version X]`):

1. Checks that it runs as root on Debian 13 (amd64) with enough free disk space. Installs `live-build`, `debootstrap`, `devscripts` and the other host tools if any are missing.
2. Checks Docker's key fingerprint, and checks that the NimbusOS APT repository answers with a correctly signed, unexpired `dists/stable/InRelease` (NOTE-04). If it doesn't, the build stops before any long-running step.
3. Gets the `.deb` files: builds them with `tools/build-debs.sh`, takes them from `--debs-dir` (CI), or with `--from-repo` downloads them with `apt-get download` through a temporary APT configuration that uses `Signed-By` with the NimbusOS key. It then checks that all seven are present, share one version, and carry the committed archive key and Docker's key. They go into `build/work/config/packages.chroot/`.
4. Prepares `build/work/`: copies `build/config/`, fills in `@NIMBUS_*@` tokens from `nimbus.conf`, and extracts the GRUB theme from the `nimbusos-branding` package, so live and installed boot share one theme. live-build itself copies the bootloader overlays over its own templates and renders `config/bootloaders/splash.svg` (verified in live-build 1:20250505).
5. Runs `lb clean`, `lb config` (every flag explained in `build/lib/common.sh`, where `tools/verify-trixie.sh` reuses the same list) and one `lb build`, with `SOURCE_DATE_EPOCH` set from the git commit.
6. Checks the finished image's APT setup by reading `filesystem.squashfs` with `unsquashfs`. The image must contain `/etc/apt/sources.list.d/docker.list` from `docker.list.binary`, with every line `signed-by=/usr/share/keyrings/docker-archive-keyring.gpg`. It must contain no trace of `docker.list.chroot`, no second Docker source, no Docker key in `trusted.gpg.d`, and the packaged keyring must hold exactly Docker's key. It also lists the live initramfs (`binary/live/initrd.img*`) with `lsinitramfs`: it must hold the NimbusOS Plymouth theme and Plymouth's script plugin, or the boot splash would not be NimbusOS (NOTE-62). Then it renames the output to `nimbusos-amd64.iso`, writes `nimbusos-amd64.iso.sha256`, and says whether the ISO is over GitHub's 2 GiB asset limit (release.yml then splits it, NOTE-41).

**Version mapping** (one version for all seven packages, taken from git; tested by `tools/tests/deb-version.test.sh` with real `dpkg`):

| Git ref | Debian version | APT suite | `NIMBUSOS_CHANNEL` in os-release |
|---|---|---|---|
| `v0.2.0` | `0.2.0` | `stable` | `stable` |
| `v0.2.1-beta.3` | `0.2.1~beta.3` | `beta` | `beta` |
| 5 commits after `v0.2.0`, untagged | `0.2.0+git20261117.5.g1a2b3c4` | not published | `dev` |

`debian/changelog` in each package holds the history. `tools/build-debs.sh` adds the top entry for the computed version at build time. It writes that entry itself rather than through `dch`, dated with the commit time, so rebuilding a commit gives identical packages. The result is checked with `dpkg-parsechangelog`.

---

## 3. Packages

| Package | Ships | Key relationships | Maintainer-script duties |
|---|---|---|---|
| `nimbusos-archive-keyring` | `/usr/share/keyrings/nimbusos-archive-keyring.gpg` (every `keys/*.asc`, so it can hold old + new keys during rotation) | none | none |
| `nimbusos-docker-keyring` | `/usr/share/keyrings/docker-archive-keyring.gpg`: Docker's key, the `Signed-By` of the image's `docker.list` (NOTE-05) | none | none |
| `nimbusos-release` | `/usr/lib/os-release` (diverted, NOTE-36); `/etc/apt/sources.list.d/nimbusos.sources` (deb822, `Signed-By`); `/etc/apt/preferences.d/nimbusos.pref` + `nimbusos-docker.pref`; `/etc/apt/apt.conf.d/21nimbusos-periodic` + `52nimbusos-unattended-upgrades`; `/usr/bin/nimbus-update` + man page; `nimbus-update-check.timer`, `nimbus-update-auto.timer`, `nimbus-flathub-setup.service` | Depends: `nimbusos-archive-keyring (>= ver)`, `python3`, `python3-apt`, `unattended-upgrades`, `gpgv` | preinst: `dpkg-divert --rename` of base-files' os-release. postinst: create `/etc/nimbusos/update.conf` if missing; recreate the beta source if Beta is selected. postrm: remove the diversion; on purge, remove the generated files |
| `nimbusos-branding` | GRUB theme + `/etc/default/grub.d/10-nimbusos.cfg`, Plymouth theme, SDDM theme "nimbus" + `/etc/sddm.conf.d/10-nimbusos.conf`, Calamares branding, Nimbus Sky wallpaper, Plasma global theme `org.nimbusos.desktop` (with `layout.js` and splash), `NimbusDark.colors`, `/etc/xdg` defaults, `/etc/xdg/nimbusos/kcm-about-distrorc` (NOTE-55), the hicolor `nimbusos` icon, the desktop-base theme `/usr/share/desktop-base/nimbusos-theme/` | Depends: `desktop-base`, `papirus-icon-theme`, `breeze-cursor-theme`, `fonts-inter`, `fonts-jetbrains-mono`, `plymouth`, `plymouth-label`, `qt6-svg-plugins` | postinst: register `nimbusos-theme` as desktop-base's `desktop-theme` alternative at priority 80, which every desktop-base background, login and GRUB alternative follows (NOTE-56); select the Plymouth theme unless the administrator chose one; `update-grub` if GRUB is installed; new systems start SDDM on Plasma (Wayland). Initramfs rebuilt by trigger. prerm: remove the alternative. postrm: hand Plymouth back; `update-grub` |
| `nimbus-control` | backend, frontend, `nimbus-privd`, units, Samba drop-in, polkit action, launcher | Depends: the Python stack (`python3-fastapi`, `python3-uvicorn`, `python3-websockets`, `python3-multipart`, `python3-docker`, `python3-libvirt`, `python3-psutil`, `python3-jinja2`, `python3-yaml`, `python3-jsonschema`, `python3-systemd`); `fonts-inter`, `fonts-jetbrains-mono` (served to the UI from their own paths, so the OFL license stays with Debian's packages); Docker (`docker-ce`, `docker-ce-cli`, `docker-compose-plugin`); libvirt and QEMU (`libvirt-daemon-system`, `libvirt-daemon-driver-qemu`, `libvirt-daemon-config-network`, `libvirt-clients`, `qemu-system-x86`, `qemu-utils`, `ovmf`, `swtpm-tools`); consoles (`novnc`, `websockify`); NAS (`samba`, `samba-common-bin`, `samba-vfs-modules`, `smbclient`, `wsdd2`, `avahi-daemon`); storage (`mergerfs`, `smartmontools`, `parted`, `xfsprogs`, `btrfs-progs`, `e2fsprogs`); `iptables`, `pkexec`, `chromium`. Recommends: `snapraid` (NOTE-26) | sysusers.d: users `nimbus-control` and `nimbus-guest`, groups `nimbus-control` and `nimbus-smb`; `nimbus-control` joins `docker`, `libvirt` and `nimbus-smb` (NOTE-25). tmpfiles.d: its directories. postinst: the smb.conf include on every configure (NOTE-23). On first install only: the `adduser.conf` keys (NOTE-09), nmbd off, and libvirt's default network on autostart. Starts units only through `deb-systemd-invoke`, and restarts them only when the package's files changed (NOTE-61). State migrations run as the service's ExecStartPre (NOTE-59). prerm: stops the units on removal. postrm (purge): removes only the Control Center's own state, caches and Samba configuration, never user data, and prints what was kept (NOTE-58) |
| `nimbus-catalog` | `/usr/share/nimbus-control/catalog/<app-id>/` (manifest with its own `template_version`, compose template, icon) and `manifest.schema.json` | Enhances: `nimbus-control`. Build-Depends: `python3-jsonschema` for the build-time check | none at all, so installing or upgrading it can't touch installed stacks. A bad manifest fails the build (NOTE-60). The backend compares versions and marks installed stacks with "Template update available" |
| `nimbusos-desktop` | nothing but docs | Depends: `${nimbus:ListDepends}` (generated; 103 packages as of deliverable 3) + the six packages above at `(>= ver)` | none |

Rules applied to all seven: lintian-clean or a justified override; idempotent, upgrade-safe maintainer scripts; user-editable files are conffiles or `.d` drop-ins; services restarted with `deb-systemd-invoke` only when their files changed (`dh_installsystemd --no-restart-after-upgrade` plus a checksum comparison in postinst).

---

## 4. Runtime architecture (installed system)

### 4.1 Processes and ports

| Unit | Runs as | Listens | Purpose |
|---|---|---|---|
| `nimbus-control.service` | `nimbus-control` (groups `docker`, `libvirt`, `nimbus-smb`) | `127.0.0.1:7777` | uvicorn + FastAPI; serves the frontend, noVNC files and fonts |
| `nimbus-privd.socket` / `.service` | root | `/run/nimbus-privd.sock` (0660 `root:nimbus-control`) | privileged verbs; socket-activated |
| `nimbus-websockify.service` | `nimbus-control` | `127.0.0.1:6080` | one websockify instance with a TokenFile plugin directory |
| VM consoles | libvirt | `127.0.0.1:59xx` (autoport) | VNC per VM with a random password (NOTE-16) |
| `nimbus-snapraid-sync.timer` | root | — | nightly sync, weekly scrub (only when parity is configured) |
| `nimbus-recycle-clean.timer` | root | — | empties `.recycle/` past each share's retention |
| `nimbus-netfix.service` | root | — | keeps libvirt guests forwarding while Docker is running (NOTE-19) |
| `nimbus-update-check.timer`, `nimbus-update-auto.timer` | root | — | NimbusOS update policy (NOTE-34) |
| `smbd`, `wsdd2`, `avahi-daemon` | Debian defaults | 445, 3702 (WS-Discovery), 5353 (mDNS) | NAS; `nmbd` off by default (NOTE-51) |

### 4.2 Paths and state

| Path | Owner / mode | What |
|---|---|---|
| `/usr/lib/nimbus-control/` | root | `python/` (`nimbus_control`, `nimbus_common`, `nimbus_privd`, byte-compiled by dh_python3), `privd/nimbus-privd.py`, `www/` (the frontend), `bin/`, `migrations/` |
| `/usr/share/nimbus-control/catalog/` | root | catalog (`nimbus-catalog`) |
| `/var/lib/nimbus-control/` | `nimbus-control` 0750 (tmpfiles.d) | state files at the top (`schema_version`, `settings.json`, `shares.json`, `stacks.json`); `backups/`, the last 5 copies made before a migration; `stacks/<stack>/`, app data, which migrations never touch and purge keeps (NOTE-58, NOTE-59) |
| `/var/lib/nimbus-vms/{isos,images}/` | isos: `root:nimbus-smb` 2775, like a share, because an `isos` share may be bind-mounted onto it; images: root 0711, libvirt pool `nimbus-images` | ISO library (NOTE-27), qcow2 disks |
| `/var/lib/nimbus-privd/` | root 0700 | SMB user registry and fstab backups (kept on purge); `package/`: the original `adduser.conf` and a checksum of the package's files (removed on purge) |
| `/var/cache/nimbus-privd/` | root 0700 | SMART/parity cache |
| `/run/nimbus-control/token` | `nimbus-control:nimbus-control` 0640 | per-boot API token (tmpfs, so it is new every boot; the directory comes from tmpfiles.d) |
| `/run/nimbus-control/vnc-tokens/` | `nimbus-control` 0750 | websockify's TokenFile entries, one per open VM console |
| `/etc/samba/nimbus/nimbus.conf` | conffile | static secure globals; includes the two generated files |
| `/etc/samba/nimbus/settings.conf`, `/etc/samba/nimbus-shares.conf` | generated by privd | workgroup/NetBIOS/guest mapping; the shares |
| `/etc/avahi/services/smb.service` | generated by privd | `_smb._tcp` (+ `_adisk._tcp` when a Time Machine share exists) |
| `/etc/nimbus/snapraid.conf` | generated by privd | SnapRAID config (NOTE-26) |
| `/etc/fstab` | managed block between `# BEGIN nimbus` / `# END nimbus` | data/parity disks (`UUID=`, `nofail`), mergerfs pool, ISO bind mount |
| `/mnt/disks/<label>`, `/mnt/parity/<label>`, `/mnt/user` | — | data disks, parity disks, mergerfs pool |
| `/srv/nimbus/shares/` | root:`nimbus-smb` 2775 | single-disk mode on the system disk |
| `/etc/nimbusos/update.conf` | created once by postinst | `CHANNEL=stable\|beta`, `POLICY=automatic\|notify\|off` |
| `/var/lib/nimbusos/update-status.json` | written by `nimbus-update` | last check, pending updates, classified errors |

### 4.3 Security model

- **Browser to `nimbus-control`.** Every HTTP and WebSocket request must carry the per-boot token: `Authorization: Bearer` for HTTP, a `Sec-WebSocket-Protocol` value for WebSockets. `Host` must be `127.0.0.1:7777` or `localhost:7777`, and `Origin` (when sent, and always for WebSockets) must match. The server sends a strict CSP (`default-src 'self'`) and `frame-ancestors 'self'`. The launcher never puts the token on a command line (NOTE-14).
- **`nimbus-control` to privd.** privd accepts a connection only when `SO_PEERCRED` shows the `nimbus-control` uid. Desktop users in the `nimbus-control` group can reach the socket file but are refused. privd checks every verb against a fixed allowlist (NOTE-15), re-validates every argument with its own validators, renders all config files itself from structured input (the backend never sends config text), calls programs with argument lists (absolute paths, never a shell), and logs each verb to the journal with structured fields. Passwords are never logged.
- **`nimbus-control` hardening.** `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ReadWritePaths=` only `/var/lib/nimbus-control` and `/var/lib/nimbus-vms/isos`, plus `ProtectHome`, `PrivateTmp`, `ProtectKernel*`, `RestrictSUIDSGID`, `LockPersonality`, `SystemCallArchitectures=native`, and an empty `CapabilityBoundingSet`. No `sudo` anywhere.
- **Input validation.** Stack, share, VM and user names each match their own regex. Ports are integers from 1 to 65535. Every path is resolved with `realpath` and must stay under an allowed root, so `..` traversal is rejected. ISO uploads are written with `O_NOFOLLOW` into a directory fd. Values put into compose templates are JSON-quoted (NOTE-30).

### 4.4 Metrics and alerts (dashboard)

- One asyncio task samples every 2 s into a 1-hour ring buffer (1,800 samples per series), kept in memory only. Sampling stops 10 s after the last WebSocket client disconnects and resumes on the next connection or `GET /api/dashboard`.
- **Docker.** One asyncio reader per running container on `/containers/<id>/stats?stream=1` over the Docker socket. These are cancellable and never block the event loop. A second reader on `/events` catches `die`, `oom`, `health_status` and `restart` immediately, which is how the "alert within 4 s" requirement is met. CPU is `cpu_delta / system_delta × 100` (share of the whole host); memory is `usage − inactive_file`. Containers are grouped by `com.docker.compose.project`.
- **VMs.** One `getAllDomainStats()` call per tick. CPU % of host = Δ`cpu.time` / (Δt × host threads); CPU % of the VM's own vCPUs is also shown. Memory is `balloon.rss`, falling back to the configured memory.
- **Storage.** `statvfs` locally. SMART and parity come from privd's hourly cache; the dashboard never triggers a scan.
- **Attribution.** System & desktop = max(0, total − Docker − VMs), calculated separately for CPU and memory.
- **Alert rules** (all in `alerts.py`, NOTE-69): container exited unexpectedly (its stack's desired state is "running"), restart loop (≥ 3 restarts in 5 min, or `Restarting`), unhealthy; VM crashed or paused by an I/O error, its watchdog or a guest crash; SMART failing, reallocated or pending sectors > 0, temperature over threshold (HDD 50 °C, NVMe 70 °C by default); pool or system disk > 90 % full; parity's last sync failed, not synced for more than 48 h, or more than 10 GB unprotected; a failed unit (`nimbus-*`, `smbd`, `wsdd2`, `docker`, `libvirtd`, NOTE-20), checked every 10 s; security updates waiting, a new NimbusOS release, restart required, repository unreachable, or not checked for more than 7 days; `nimbusos-desktop` not installed (NOTE-48).

### 4.5 Storage and shares

- System disk and live medium detection: `findmnt`/`lsblk -s` walk from `/`, `/boot`, `/boot/efi`, swap and `/run/live/medium` up to their parent disks. Those disks are never offered for formatting, and privd refuses them even if asked.
- Format: `wipefs` → GPT with one partition → `mkfs.xfs` (default), `mkfs.ext4` or `mkfs.btrfs` with a label → `UUID=` fstab entry with `nofail` → mount at `/mnt/disks/<label>`. The fstab write is atomic (temp file, fsync, rename) and keeps a timestamped backup.
- Pool modes: **Single** (one data disk, or the system disk at `/srv/nimbus/shares`); **Pooled** (mergerfs fstab entry over the data disks at `/mnt/user`, `category.create=mfs` or `epmfs`); **Pooled + parity** (Pooled plus SnapRAID with 1–2 parity disks).
- Shares: privd renders `nimbus-shares.conf` from a validated spec, runs `testparm -s` on a candidate config set, swaps the file atomically, runs `smbcontrol smbd reload-config`, re-runs `testparm` on the live config, and rolls back on any failure. Share directories are `root:nimbus-smb` with setgid; each share has `force group = nimbus-smb`, `create mask = 0664` and `directory mask = 2775` (umask 0002).
- SMB-only users: `adduser --system --no-create-home --home /nonexistent --shell /usr/sbin/nologin --ingroup nimbus-smb`, password set with `smbpasswd -a -s` on stdin. They are system UIDs, so SDDM never lists them.

### 4.6 Updates

- `nimbus-update` (`status`, `check`, `install`, `channel stable|beta`, plus `policy` and `--json` for privd) holds all update logic. privd calls it; it is also usable on its own as root.
- `check`: `apt-get update`, then sorts any failure into DNS, TLS, signature or expired metadata, using apt's own messages. A bad signature is always reported as an error, never as "no updates". Then it simulates the upgrade with python3-apt, groups pending updates as Security / NimbusOS / Debian, fetches `releases.json` and verifies its detached signature with `gpgv` (NOTE-38), and writes `/var/lib/nimbusos/update-status.json`.
- `install`: run by privd as a transient unit (`systemd-run --unit=nimbus-update-<id>`) so it survives restarts of the services it upgrades (NOTE-35). `apt-get update` then `apt-get full-upgrade` with `DEBIAN_FRONTEND=noninteractive`, `--force-confdef` and `--force-confold`, `UCF_FORCE_CONFFOLD=1`, and `-o DPkg::Lock::Timeout=…` so it waits for unattended-upgrades instead of colliding with it. The unit's output goes to the journal. privd streams it to the UI drawer, and after a reconnect re-reads it with `journalctl -u nimbus-update-<id>`.
- Channels: Stable is the packaged `nimbusos.sources`. Beta adds a generated `nimbusos-beta.sources` (same `Signed-By`). See NOTE-33.
- Policy: unattended-upgrades handles Debian-Security only. NimbusOS updates follow `POLICY` through nimbus-update's own timers (NOTE-34). The system never reboots on its own.
- If `nimbusos-desktop` is missing, the Updates page warns: "This system no longer receives new NimbusOS software. Reinstall nimbusos-desktop" (from `status.desktop_metapackage_installed`, NOTE-48).

---

## 5. Assumptions

| # | Assumption |
|---|---|
| A1 | The GitHub repository stays public, Pages is served from the `gh-pages` branch root, and the URLs in the header table are final. Both URLs are in `build/nimbus.conf`. |
| A2 | Builds run on a Debian 13 host or a privileged `debian:trixie` container with network access and at least 40 GB free. GitHub runners need their preinstalled toolchains removed first to make room (done in `release.yml`). |
| A3 | Docker publishes `docker-ce`, `docker-ce-cli`, `containerd.io` and `docker-compose-plugin` for `https://download.docker.com/linux/debian trixie stable`, signed by the key with fingerprint `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`. |
| A4 | **Confirmed** (live-build `1:20250505+deb13u1`, Calamares 3.3.14, `calamares-settings-debian` 13.0.13; §7). live-build supports `config/includes.chroot_after_packages/`, `config/archives/*.{list,key}.{chroot,binary}` and the `grub-pc` / `isolinux` / `syslinux_common` overlays. Debian's settings provide the `bootloader-config` and `sources-media` steps. |
| A5 | **Wrong.** Trixie's `reprepro` is 5.3.1 (`5.4.6+really5.3.2`), which has `ValidFor:` but no `Limit:`. See NOTE-52. |
| A6 | Debian's signed GRUB finds the `grub.cfg` that sits next to it whatever the EFI directory is called (checked by smoke test 1; fallback in NOTE-07). |
| A7 | The UI is English-only; the live session uses `en_US.UTF-8` and a `us` keyboard (both tunable in `nimbus.conf`); Calamares lets the user change both. |
| A8 | Minimum hardware: x86-64-v1 CPU with VT-x/AMD-V for the VM tab, 4 GB RAM, a 40 GB system disk. |
| A9 | Default hostname `nimbusos`; connection strings and dashboard links use the machine's actual hostname, so a renamed machine still gets correct links. |
| A10 | The maintainer generates the archive signing key offline as described in `docs/SIGNING.md` and commits only the public key (NOTE-44). |
| A11 | Every package's Maintainer comes from `NIMBUS_MAINTAINER` in `build/nimbus.conf`, the only file that contains the maintainer's address. It reaches `debian/control` and the changelog trailers from there. Commit trailers, code comments, docs and test data never contain it; tests use `test@example.invalid`. |
| A12 | Licensing: GPL-3.0-or-later for code and packaging, CC-BY-SA-4.0 for artwork (wallpaper, themes), machine-readable DEP-5 `debian/copyright`. The Inter and JetBrains Mono font files are never copied into the repo; they stay in Debian's packages under their OFL license. |

---

## 6. NOTE: deviations, limitations and interpretations

### Build and image

**NOTE-01 — Rounded window corners.** In Trixie's Plasma, the Breeze window decoration rounds only the title-bar (top) corners. KWin has no built-in option to round all corners, and the third-party effects that do are not in Debian. NimbusOS therefore keeps Breeze's rounding and adds nothing more.

**NOTE-02 — `includes.chroot` folder name.** Current live-build reads live-only files from `config/includes.chroot_after_packages/` (copied after package installation, overriding packaged files inside the live image). NimbusOS uses that name instead of the older `config/includes.chroot/`.

**NOTE-03 — One Docker source, no conflicting `Signed-By` (single `lb build`; verified against live-build's code).** apt aborts when two entries for the same repository carry different `Signed-By` values. To make that impossible, no package ships a Docker source, so there is only ever one entry, and it comes from live-build's `config/archives/`.

What live-build `1:20250505+deb13u1` does (its `chroot_archives` script, read in deliverable 3):
- **Build time:** it writes `docker.list.chroot` as `sources.list.d/docker.list` and copies `docker.key.chroot` to `trusted.gpg.d/docker.key.chroot.asc`.
- **End of the chroot stage:** it deletes both of those itself, then writes `docker.list.binary` as `sources.list.d/docker.list`.
- **Image:** the final source names `signed-by=/usr/share/keyrings/docker-archive-keyring.gpg`, which the `nimbusos-docker-keyring` package ships, so Docker key rotations still arrive as NimbusOS updates.

Because live-build only places keys at that fixed `trusted.gpg.d/<name>.asc` path, the other option (the same `Signed-By` path in a packaged source) can't work without running stages by hand, so it is ruled out. The planned cleanup hook `0150` is not needed. `build.sh` instead checks the finished image (§2, step 6): `docker.list` must come from `docker.list.binary` and carry `Signed-By` on every line, there must be no `docker.list.chroot`, and no build-only or globally trusted Docker key. The trade-off: the source line itself (Docker's URL) can't be changed by a package update, which matters only if Docker ever moves its repository. `build.sh` runs `lb clean`, `lb config` and one `lb build`.

**NOTE-04 — The first ISO build needs a published APT repository.** When live-build switches the image to its final APT sources at the end of the chroot stage, it refreshes the indices. `nimbusos-release` is installed by then, so the NimbusOS URL has to serve a signed repository; an empty one is enough. Before building, `build.sh` downloads `dists/stable/InRelease`, verifies it with `gpgv` against the committed archive key, and checks `Valid-Until`. On any failure it stops with a plain message, for example "The NimbusOS APT repository does not answer … run the refresh workflow once (Actions → refresh → Run workflow), then build again." `release.yml` publishes to APT before it builds the ISO. For the very first setup, `refresh.yml` creates an empty signed repository when `gh-pages` doesn't exist yet (`docs/RELEASING.md`, one-time setup).

**NOTE-05 — Docker's key and pinning come from NimbusOS packages.** Docker's key ships as `/usr/share/keyrings/docker-archive-keyring.gpg` in its own package, `nimbusos-docker-keyring`, which `nimbusos-desktop` depends on. Its fingerprint, `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`, is pinned in `nimbus.conf` and written in the comments of both Docker source files.

`tools/fetch-docker-key.sh` (deliverable 4) manages the one copy in the repository, `build/config/archives/docker.key.chroot`:
- **`--write`** downloads the key from `https://download.docker.com/linux/debian/gpg` over HTTPS only and writes it only if it holds exactly the pinned key. The file stays byte-identical to Docker's download, because live-build copies it unchanged into the chroot's `trusted.gpg.d`.
- **`--verify-local`** (offline, CI on every push) checks the key and the fingerprint comments, that `docker.list.binary` uses `signed-by=` the packaged keyring, that `docker.list.chroot` has no `signed-by`, and the marker lines.
- **`--check`** (network) compares the committed key material with what Docker serves now, so a key rotation is noticed early. It runs in a weekly scheduled job in `refresh.yml` (deliverable 10). If the key changed, the job fails and opens a GitHub issue ("Docker's APT signing key changed"), at most one open issue at a time, using the `gh` CLI with `issues: write` for that job only. The workflow never updates the key itself: a person runs `tools/fetch-docker-key.sh --write`, reviews the change and releases it.

`build.sh` and `tools/build-debs.sh` check the fingerprint before every build. Both sources carry `arch=amd64`, so adding a foreign architecture later (for example i386) doesn't make apt look for Docker packages Docker doesn't build. `nimbusos-release` ships `nimbusos-docker.pref`, which pins the Docker origin the same way as NimbusOS's: priority 1 for everything, 500 for `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin`, `docker-buildx-plugin`, `docker-ce-rootless-extras` and `docker-model-plugin`. That list was taken from the 11 packages Docker's `trixie` index actually contains: `docker-ce` 29.x recommends `docker-ce-rootless-extras`. Docker's repository therefore cannot replace Debian packages, and `packages/nimbusos-release/tests/apt-integration.test.sh` proves it (§7).

**NOTE-06 — Calamares configuration is live-only.** Calamares runs only in the live session, and NimbusOS's `packages.conf` removes it from the installed system: Debian's list removes the live packages and `calamares-settings-debian` but leaves `calamares` itself installed, so NimbusOS adds it (NOTE-63). `settings.conf` and the module overrides therefore live in `includes.chroot_after_packages/`, where they replace `calamares-settings-debian`'s copies inside the live image. The branding component `/usr/share/calamares/branding/nimbusos/` ships in `nimbusos-branding`, as specified. Debian's `bootloader-config` and `sources-media` steps are kept. Debian's `sources-final` is replaced by `nimbus-sources-final`, which writes deb822 `/etc/apt/sources.list.d/debian.sources` with `main contrib non-free non-free-firmware` for `trixie`, `trixie-updates` and `trixie-security`, and empties the legacy `sources.list`. The NimbusOS and Docker sources are already on the target, because `nimbusos-release` is part of the copied image. A final `nimbus-target-cleanup` step deletes the live-only files from the target. `users.conf` uses the Calamares 3.3 `hostname:` block with `template: "nimbusos"`.

**NOTE-07 — Secure Boot and the EFI directory.** Installed systems boot through Debian's signed shim and GRUB, which NimbusOS does not rebuild, so Secure Boot keeps working. `/etc/default/grub.d/10-nimbusos.cfg` sets `GRUB_DISTRIBUTOR=NimbusOS`. That makes the boot entries read "NimbusOS" and makes `grub-install` use `EFI/nimbusos/`. Calamares' `efiBootloaderId` is set to the same value, so the installer and later GRUB package upgrades agree, and `installEFIFallback: true` also writes `EFI/BOOT/BOOTX64.EFI`. This depends on assumption A6. If smoke test 1 fails with Secure Boot on, the fallback is `GRUB_DISTRIBUTOR=Debian`: the EFI directory stays `EFI/debian`, but boot entries then read "Debian GNU/Linux".

**NOTE-08 — The SDDM greeter stays on X11.** SDDM's Wayland greeter is less dependable in VMs and on some GPUs, so the login screen uses Debian's default X11 greeter with the NimbusOS QML theme. The **session** defaults to Plasma (Wayland): the live autologin uses `Session=plasma`, and new installs get SDDM's last-session state pre-set to `plasma.desktop`. Plasma (X11) remains selectable.

**NOTE-09 — `/etc/adduser.conf`.** The file belongs to the `adduser` package and has no drop-in directory. The `nimbus-control` postinst therefore sets exactly two keys in place, `EXTRA_GROUPS="docker libvirt kvm nimbus-control"` and `ADD_EXTRA_GROUPS=1`. It does this on the first install only, so an administrator's later change survives upgrades. It keeps a copy of the original file in `/var/lib/nimbus-privd/package/`, and on purge puts the original lines back where the keys still have the values it set (tested: Debian's file comes back byte-identical). This is a deliberate, documented exception to Debian Policy §10.7.4. `EXTRA_GROUPS` only applies to accounts made with `adduser`. Plasma's Users settings page creates accounts through AccountsService and `useradd`, which ignore it. For such users the launcher detects the missing group and offers **Grant access**, which asks for an administrator password through polkit; the user then logs out and back in.

**NOTE-10 — How Plasma theme updates reach existing users.** The global theme, panel layout and wallpaper are applied at a user's first login. After that, Plasma stores them per user, and NimbusOS does not overwrite them. Colors, accent, icons, fonts, widget style and KWin effects come from `/etc/xdg` defaults, so existing users do get updates there unless they changed those settings themselves.

**NOTE-11 — Flathub.** The `.flatpakrepo` file carries Flathub's signing key. It is downloaded over HTTPS, which is Flathub's documented method. Writing that key into this repo from memory would be inventing it. `nimbus-flathub-setup.service` (in `nimbusos-release`) therefore runs `flatpak remote-add --if-not-exists --system flathub https://dl.flathub.org/repo/flathub.flatpakrepo` once after the network is up, and the build hook `0100` runs the same packaged script, so the ISO already contains Flathub. Systems that get NimbusOS through an update receive it at their next boot with a network connection.

**NOTE-12 — No binary assets.** All artwork is SVG.
- **GRUB, Plymouth, SDDM, Calamares:** they need PNG files, so `nimbusos-branding` renders them during the package build (`rsvg-convert` from `librsvg2-bin`). GRUB fonts are generated with `grub-mkfont` from `fonts-inter`.
- **Live boot splash:** `build/config/bootloaders/splash.svg` is rendered by live-build itself inside the chroot (640×480 for ISOLINUX, 800×600 for GRUB), so the build host needs no SVG tools. Note that live-build fills its own `@PROJECT@` token with "Debian GNU/Linux", so the NimbusOS SVG doesn't use it.
- **Plasma wallpaper:** the package ships the SVG directly.

**NOTE-13 — Web UI fonts come from Debian packages.** The UI uses Inter and JetBrains Mono from `fonts-inter` and `fonts-jetbrains-mono`, which are Depends of `nimbus-control`. The backend serves the font files straight from those packages' paths under `/usr/share/fonts/` at `/assets/fonts/*`, finding them at startup in the packages' file lists so a path change in a Debian update doesn't break it. The fonts are local and work offline with no CDN. No font file is copied into the repo, so the OFL licensing stays with Debian.

### Control Center

**NOTE-14 — The launcher passes a one-time launch code, not the token.** A token on the Chromium command line would be readable by every local user through `/proc/<pid>/cmdline`. `nimbus-control-open` reads `/run/nimbus-control/token` and trades it with the backend for a launch code. The code stops working after its first use or 30 seconds, whichever comes first. The launcher then runs `chromium --app=http://127.0.0.1:7777/?launch=<code> --class=NimbusControl --user-data-dir=<per-user profile>`. The page trades the code for the session token, keeps the token in `sessionStorage`, and removes the code from the address bar. If the user is not in `nimbus-control`, the launcher explains this and offers **Grant access** (NOTE-09).

**NOTE-15 — The privd verb allowlist is longer than the example list.** In addition to the 15 verbs named in the spec: `disk.unmount`, `share.delete_data`, `smbuser.list`, `samba.settings` (workgroup, description, SMB on/off, NetBIOS), `samba.test` (`smbclient -L localhost` with a temporary 0600 auth file, so the password is never on a command line), `snapraid.status`, `snapraid.sync_now`, `update.status`, `update.history` (`/var/log/apt/history.log` is not readable by unprivileged users), `stack.prepare_volume` and `stack.delete_data` (NOTE-31).

**NOTE-16 — VNC passwords.** A VNC port bound to localhost is still reachable by every local process. Each VM therefore gets a random VNC password, limited to 8 characters by the VNC protocol, which the backend hands to the embedded noVNC.

**NOTE-17 — The Control Center app window on Wayland.** `--class=NimbusControl` sets the X11 `WM_CLASS`, and `StartupWMClass=NimbusControl` matches it. In a Wayland session Chromium sets its own app_id, so Plasma may show the window as a separate task entry next to the pinned launcher. Nothing else is affected.

### Apps, VMs and networking

**NOTE-18 — Pi-hole and port 53.** libvirt's dnsmasq listens on `192.168.122.1:53`. The Pi-hole template therefore binds DNS to the host's LAN IP (`<lan-ip>:53:53/tcp+udp`, detected from the default route). If no LAN IP can be found, or another process already holds port 53 on it, the install stops with a message that says which and how to fix it.

**NOTE-19 — Docker blocks libvirt forwarding.** Docker sets the iptables `FORWARD` policy to `DROP`, which breaks forwarded traffic for libvirt guests. `nimbus-netfix.service` (After/PartOf `docker.service`) adds `DOCKER-USER` accept rules for `virbr+` interfaces, as Docker's documentation describes for routing other traffic through the host.

**NOTE-20 — libvirt daemons (checked in Trixie, deliverable 5).** libvirt 11.3 in Trixie runs the monolithic `libvirtd`: `libvirtd.service`, `libvirtd.socket` and `virtlogd.socket` are enabled by `libvirt-daemon-system` itself, and the modular `virtqemud` units aren't shipped. So the postinst enables nothing for libvirt, and the service-health alert checks `libvirtd`. `libvirt-daemon-config-network` installs the `default` NAT network but leaves `autostart/` empty. On first install the postinst therefore creates `/etc/libvirt/qemu/networks/autostart/default.xml` → `/etc/libvirt/qemu/networks/default.xml`, the same link `virsh net-autostart` makes, which needs no running daemon and takes effect when libvirtd next starts.

**NOTE-21 — The live session.** The live root filesystem is an overlay, which Docker's `overlay2` storage driver cannot sit on. Two live-only mount units therefore put `/var/lib/docker` and `/var/lib/containerd` on tmpfs (containerd's overlayfs snapshotter has the same problem, and Docker can keep images there): apps work but are gone at shutdown. Both carry `ConditionKernelCommandLine=boot=live`, so even if one were left on an installed system it would not mount (NOTE-63). Disk formatting and pool changes are disabled behind the banner "Install NimbusOS to set up storage"; read-only views work.

**NOTE-22 — No firewall.** NimbusOS ships without an active firewall. If you enable one, allow SMB with `ufw allow 445/tcp`, or for firewalld `firewall-cmd --permanent --add-service=samba && firewall-cmd --reload`. Settings shows the same text.

### Storage and Samba

**NOTE-23 — A single include line in `smb.conf`.** Debian's `smb.conf` has no include directory, and Samba reads nothing else. The `nimbus-control` postinst appends one marked block to the end of `/etc/samba/smb.conf`: `include = /etc/samba/nimbus/nimbus.conf`. Everything else NimbusOS sets lives in `/etc/samba/nimbus/` (static globals as a conffile; generated `settings.conf`) and in `/etc/samba/nimbus-shares.conf`. Debian's ucf handling of `smb.conf` otherwise stays as it is. The updater sets `UCF_FORCE_CONFFOLD=1` so a Samba upgrade doesn't replace the file. A CI test (`control-center/tests/integration/samba-ucf-keeps-include.sh`, in `debian:trixie`) checks that this works. It installs `samba`, appends the include, changes the packaged template, runs the same ucf update a Samba upgrade runs (with `--force-confold` / `UCF_FORCE_CONFFOLD=1`), and fails if the include line is gone. privd's repair is only the fallback: it checks for the include before each `share.apply` and on startup, puts it back if it is missing, and raises an alert when it does.

**NOTE-24 — Samba's own mDNS registration is turned off.** `multicast dns register = no` stops smbd from advertising itself through Avahi. The generated `/etc/avahi/services/smb.service` then becomes the only advertisement, which avoids duplicate entries in Finder and lets NimbusOS add the `_adisk` record for Time Machine.

**NOTE-25 — A dedicated guest account.** Public shares should be writable by guests without world-writable folders. The package therefore creates a `nimbus-guest` system user (nologin, no home, member of `nimbus-smb`) and sets it as Samba's `guest account`. `map to guest = Bad User` is set only while at least one Public share exists.

**NOTE-26 — SnapRAID.** `/etc/snapraid.conf` may belong to Debian's snapraid package, so NimbusOS writes `/etc/nimbus/snapraid.conf` and always calls `snapraid -c`. Parity is **scheduled, not real time**: nightly sync, and a weekly scrub of 8 % of the array covering blocks older than 10 days. The pool card and the wizard both say so. privd refreshes SMART data hourly with `smartctl -n standby`, so spun-down disks are not woken. Availability: confirmed. `snapraid` 12.4-1 is in Trixie main (§7), so the planned fallback package is not needed. `nimbus-control` still only Recommends `snapraid`. Whenever `/usr/bin/snapraid` is missing, the backend reports no parity support, and the NAS wizard and pool settings hide "Pooled + parity" instead of offering it and then failing.

**NOTE-27 — The ISO library when an `isos` share exists.** The VM tab always uses `/var/lib/nimbus-vms/isos`. When an `isos` share exists, privd bind-mounts the share's folder onto that path, through a managed fstab line. `nimbus-control` then keeps one narrow `ReadWritePaths=` entry, and ISOs dropped in over SMB show up in the VM wizard.

### Catalog

**NOTE-28 — Catalog icons.** Each app gets a neutral monogram SVG (initials on the app's category color), not the vendor's logo. This avoids trademark problems and binary files.

**NOTE-29 — Nextcloud AIO and Vaultwarden are exceptions.** Nextcloud AIO requires the container name `nextcloud-aio-mastercontainer`, the named volume `nextcloud_aio_mastercontainer` (its data is not under the stack directory) and the Docker socket. It also starts its own `nextcloud-aio-*` containers, which the dashboard groups under the AIO app. Vaultwarden's web vault needs a secure context, so its manifest sets `webui.secure_context: true` and its Open link uses `http://localhost:<port>` instead of the hostname. For LAN access, the notes point to Nginx Proxy Manager with TLS. (Deliverable 9 implements this with three manifest fields: `single_instance`, `companions` and `webui.scheme`; see NOTE-70.)

**NOTE-30 — Compose templates and the raw editor.** Every value inserted into a `.j2` template goes through a JSON-quoting filter (`| q`) in a Jinja2 `SandboxedEnvironment` with `StrictUndefined`. That rules out YAML injection. In the raw editor, a bind mount must resolve inside the stack directory, inside a share, or to one of the host paths listed in the app's manifest (for example `/var/run/docker.sock` for Portainer and AIO, `/run/dbus` for Home Assistant). Anything else is rejected with a message that says which path broke the rule.

**NOTE-31 — Stack data owned by container users.** Containers write their data as root or as their own UID (Grafana uses 472), which `nimbus-control` cannot change or delete. `stack.prepare_volume` (chown to the UID in the manifest) and `stack.delete_data` are privd verbs, limited to paths inside `/var/lib/nimbus-control/stacks/<stack>/`.

### Updates and releases

**NOTE-32 — Beta tags map to `~beta` versions.** A tag `vX.Y.Z-beta.N` becomes Debian version `X.Y.Z~beta.N`, so the final `X.Y.Z` sorts above all of its betas. (`X.Y.Z-beta.N` would be an invalid native version, and Debian would sort it above `X.Y.Z`.)

**NOTE-33 — Switching from Beta back to Stable doesn't downgrade.** Beta adds the `beta` suite on top of `stable`, both at priority 500, so apt picks the highest version. Switching back to Stable removes the `beta` suite, and the system keeps its beta versions until Stable passes them. The confirmation dialog says this, and `docs/RELEASING.md` shows an explicit downgrade with `apt install <pkg>=<version>`.

**NOTE-34 — unattended-upgrades installs security updates only.** Debian's default unattended-upgrades configuration also installs regular point-release updates. The NimbusOS drop-in (`52nimbusos-unattended-upgrades`) uses apt's `#clear Unattended-Upgrade::Origins-Pattern;` and then re-adds only the Debian-Security origins, and keeps `Automatic-Reboot "false"`. NimbusOS updates are **not** routed through unattended-upgrades, because it refuses to install new dependencies from origins outside its allowlist, and new Debian dependencies of `nimbusos-desktop` are how new software reaches existing installs. Instead, `nimbus-update-auto.timer` runs `nimbus-update auto` daily under the **Automatic** policy (`apt-get install --no-remove` of the installed `nimbus-*`/`nimbusos-*` packages, which pulls in new dependencies). The timer uses `RandomizedDelaySec=` so machines don't all hit the repository at once. `nimbus-update auto` first tries, without waiting, to take the same locks unattended-upgrades uses: unattended-upgrades' own lock file, then dpkg's `lock-frontend` and the APT lists lock, through python3-apt's `apt_pkg.get_lock`. If any is held, it logs "busy, retrying later" and exits. The service retries 30 minutes later, up to 3 times per day. It never reboots; it only sets the "restart required" state. **Notify only** runs just the daily check. **Off** runs neither, though Check now still works. Regular Debian updates arrive through **Install updates** or Discover.

**NOTE-35 — Updates run in a transient unit.** Upgrading `nimbus-control` restarts `nimbus-privd`. If apt were a child of privd, that restart would kill dpkg midway. privd therefore starts `nimbus-update install` with `systemd-run` as its own unit (`nimbus-update-<id>`, `SyslogIdentifier=nimbus-update`). All of its output goes to the journal. privd follows it with python3-systemd's journal reader and relays the lines to the UI. After a reconnect or page reload, the Updates page asks privd (`update.log`) for the unit's journal entries and shows the whole log again, finished or not.

**NOTE-36 — os-release.** `base-files` owns `/usr/lib/os-release`, so `nimbusos-release` diverts it (`dpkg-divert --rename`) and ships its own: `NAME="NimbusOS"`, `ID=nimbusos`, `ID_LIKE=debian`, `VERSION_ID`, `VERSION`, and `NIMBUSOS_CHANNEL`, a vendor-prefixed custom field, which os-release(5) allows. It keeps `ID_LIKE=debian` and `VERSION_CODENAME=trixie`, because unattended-upgrades (through `lsb_release`), Docker and other tools look up the release by codename. It adds `DEBIAN_CODENAME=trixie` for third-party scripts that look for the Debian base on derivatives. `/etc/debian_version` stays base-files' own file and isn't touched, and `/etc/os-release` stays base-files' symlink, so it points to the NimbusOS file. `lsb_release -a` therefore reports "Distributor ID: NimbusOS" with "Codename: trixie". `packages/nimbusos-release/tests/os-identity.test.sh` proves that unattended-upgrades, apt, Docker's source and Calamares still resolve the Debian release correctly (§7).

**NOTE-37 — Where release notes come from.** The spec takes release notes from the GitHub Release body, but the release is created in the last step, after `releases.json` has been published. The single source is therefore `docs/release-notes/<tag>.md`, written before tagging. `release.yml` uses that file both as the GitHub Release body and as the notes in `releases.json`, and fails with a clear message if it is missing. A line `Restart-Required: yes` in the file sets `restart_required`.

**NOTE-38 — `releases.json` is signed.** `releases.json` gets a detached signature, `releases.json.sig`, made with the archive key, and `nimbus-update` verifies it with `gpgv` against the NimbusOS keyring before showing any release notes. The notes are therefore as trustworthy as the packages.

**NOTE-39 — Pages is published from a branch.** GitHub Pages serves the `gh-pages` branch, so the workflows need only `contents: write`. `pages: write` and `id-token: write` belong to the artifact-based `deploy-pages` method, which isn't used. Each publish force-pushes `gh-pages` as a single fresh commit, which keeps the repository from growing with every release. The repository's state (the pool with the last 3 versions of each package) lives in the branch itself; it is public and contains no secrets.

**NOTE-40 — Scheduled workflows are paused after 60 days of inactivity.** GitHub disables scheduled workflows in public repositories that see no activity for 60 days. If `refresh.yml` stops, `InRelease` expires 14 days after its last signing, and installed systems show the "expired metadata" error, which explains how to fix it. `docs/RELEASING.md` explains how to re-enable the workflow.

**NOTE-41 — ISOs over 2 GiB are split.** A Plasma desktop with Docker, QEMU/libvirt, Chromium, Firefox and firmware is expected to be roughly 3.5–4.5 GB even with xz-compressed squashfs (`lb config --chroot-squashfs-compression-type xz`, set in `nimbus.conf`). That is above GitHub's 2 GiB limit per release asset. `tools/split-iso.sh` splits the ISO into 1,900 MiB parts (`nimbusos-amd64.iso.part00`, `.part01`, …). `SHA256SUMS` (and its signature `SHA256SUMS.gpg`) lists both the joined ISO and every part, so downloads can be checked before and after joining. The release body and README give join and verify commands for Linux/macOS (`cat nimbusos-amd64.iso.part* > nimbusos-amd64.iso`, `sha256sum -c --ignore-missing SHA256SUMS`) and for Windows PowerShell (a short stream-copy loop over the sorted parts, then `Get-FileHash -Algorithm SHA256`). An ISO under the limit is uploaded whole.

### CI and repository

**NOTE-42 — Actions are pinned to commit SHAs.** Workflows use only GitHub's own actions (`actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`). Each is pinned to the full commit SHA of a released tag, with the tag in a trailing comment (deliverable 10 pinned their latest releases: `# v7.0.1`, `# v7.0.2` and `# v8.0.2`), and `ci.yml` re-checks the pins with `tools/pin-actions.sh --check`. Releases are created with the preinstalled `gh` CLI, and no marketplace actions are used. The SHAs are resolved over the network in deliverable 10 by `tools/pin-actions.sh`, which you can re-run later. `.github/dependabot.yml` (ecosystem `github-actions`) opens PRs that move the pins and their comments forward.

**NOTE-43 — ruff is a CI tool only.** ruff is installed in `ci.yml`'s lint job, a `debian:trixie` container, with Debian's pipx at a pinned version (`RUFF_VERSION`, 0.17.6), and with the same pin locally (`tools/lint.sh`). It is not part of the image or of any package, and `tools/verify-trixie.sh` doesn't check for it.

**NOTE-44 — Bootstrapping the signing key.** The repository can only hold the public key after you have generated it (`docs/SIGNING.md`). Until `packages/nimbusos-archive-keyring/keys/` contains a key, `ci.yml` builds the packages with a throwaway key generated inside the job and prints a warning. `release.yml` refuses to run without a committed public key and checks that its fingerprint matches the `APT_SIGNING_KEY` secret.

**NOTE-45 — Desktop-user groups and the `docker` group.** As specified, desktop users (live, Calamares and `adduser`) join `docker`, `libvirt`, `kvm` and `nimbus-control`. Membership in `docker` or `nimbus-control` is effectively root. `README.md` says this plainly, and the Control Center is meant for the machine's administrator.

**NOTE-46 — README location.** `README.md` sits at the repository root, which is what GitHub shows on the project page. `docs/` holds `RELEASING.md`, `SIGNING.md` and `TESTING.md`.

**NOTE-47 — Package build layout.** `packages/nimbus-control` and `packages/nimbus-catalog` contain only `debian/`. `tools/build-debs.sh` stages each package into `_build/src/<pkg>/`, copying `control-center/` or `catalog/` alongside, and builds there. The source trees stay where the spec puts them, and each package still builds from a self-contained directory.

**NOTE-48 — `nimbusos-desktop`: Depends versus Recommends (revised after review).** `nimbusos-desktop` gets its Depends **and** Recommends from the package lists at build time, through two substvars, so the lists and the metapackage can't drift apart. Each list's role comes from its header line:
- **No marker → Depends:** what NimbusOS needs to work. That covers `10-base`, `20-plasma` core, `30-theming-fonts`, `50-docker`, `60-virtualization` and `70-storage-nas`: 70 packages plus the six NimbusOS packages.
- **`#! nimbus: desktop-recommends` → Recommends:** what users may reasonably remove. That covers `15-firmware` (all firmware and microcode), `25-desktop-extras` (KDE apps, both browsers, Discover with PackageKit, Flatpak, Bluetooth, command-line conveniences, `kde-plasma-desktop`) and `75-storage-optional` (SnapRAID): 37 packages.
- **`#! nimbus: exclude-from-desktop` → left out:** Calamares, which removes itself after install, and `90-nimbusos`, the metapackage itself.

This splits the spec's seven lists into nine. Debian's `kde-plasma-desktop` hard-depends on `kde-baseapps` (Dolphin), so it is only recommended, and the Plasma core it depends on (`plasma-desktop`, `plasma-workspace`, `udisks2`, `upower`) is listed by name.

Why the split works:
- Removing a recommended package keeps `nimbusos-desktop` installed.
- apt never reinstalls a recommendation the user removed, but it does install packages that a later release newly recommends.
- `nimbus-update install` and `auto` always pass `-o APT::Install-Recommends=true` and never `--no-install-recommends`.

`packages/nimbusos-release/tests/recommends.test.sh` proves this with real apt and the real `nimbus-update` (§7). If `nimbusos-desktop` is missing anyway, `nimbus-update status` reports `desktop_metapackage_installed: false`. The Updates page (deliverable 8) then shows "This system no longer receives new NimbusOS software. Reinstall nimbusos-desktop", and the dashboard raises the same alert.

The `*.list.binary` files are never read: they only fill the ISO's package pool, where GRUB for BIOS and for UEFI sit in separate lists because the two conflict and the installer picks one. A package listed twice is an error. To live-build the marker lines are ordinary comments (NOTE-53).

**NOTE-49 — `nimbus.conf` values reach the packages.** Build tunables that installed systems need (the APT URL, the `releases.json` URL, the Maintainer) are written as `@NIMBUS_*@` tokens in the packaging and payload files. That includes `debian/control`, `debian/changelog` trailers, `debian/copyright`, os-release, the APT source and pin files, and `nimbus-update`'s build values. `tools/build-debs.sh` fills them in from `build/nimbus.conf` when it stages a package, so `nimbus.conf` remains the one place to change them. The maintainer's address in particular exists only in `nimbus.conf` and in the built packages' `Maintainer` field and changelog (A11). An unknown token stops the build, and `install.test.sh` checks that no token reaches an installed system.

**File modes come from git (revised after review).** Staging sets every file to 0644 and every directory to 0755, then gives 0755 to exactly the files git's index records as `100755` (`nimbus_apply_git_modes`, which reads `git ls-files -s`). Files git doesn't track are staged as 0644, with a warning. So a package is identical whether it was built from a Windows or a Linux checkout, and a Python module with a shebang stays non-executable. `tools/file-modes.conf` lists by role which files must be executable, and `tools/check-file-modes.sh` (CI) fails on any tracked file whose mode in git doesn't match that list. It printed the exact `git update-index --chmod` fix in the test where it was made to fail.

**NOTE-54 — Justified lintian overrides.** lintian 2.122 is clean for the packages built so far except for three tags on `nimbusos-release`. Each is overridden with its justification in `debian/lintian-overrides`:
- `package-installs-apt-sources`: shipping the NimbusOS source is this package's purpose (spec 4.1).
- `package-installs-apt-preferences` (×2): the pinning is the safeguard required by spec 4.2.
- `uses-dpkg-database-directly`: a false positive. `nimbus-update` only probes dpkg's `lock-frontend`, the documented frontend locking interface, without waiting.

`tools/build-debs.sh --lintian` fails on any other error or warning.

**NOTE-50 — Developing on Windows.** The repository is edited on Windows with `core.filemode=false`. `.gitattributes` forces LF line endings, and executable bits are recorded explicitly with `git update-index --chmod=+x` for every script, hook and `debian/rules`. Everything that needs Debian runs in `tools/dev-shell.sh`. On this workstation that is Podman inside the WSL distribution `nimbus-build`; any Docker or Podman host works. By default `dev-shell.sh` mounts the checkout read-only and works in a clone inside the container, which is faster than the Windows mount and has git's file modes. Uncommitted changes are applied with `git apply --index`, so new files keep their recorded modes. ISOs and `.deb` files come back to the host in `dist/` and `_build/debs/`. `--mount` runs directly in the checkout, for tools that must write into it (`fetch-docker-key.sh --write`).

### Found while building the branding (deliverable 5, part 2)

**NOTE-55 — "About this System" branding lives in `/etc/xdg/nimbusos/`.** Trixie's Contents index shows that `desktop-base` owns `/etc/xdg/kcm-about-distrorc`, so NimbusOS can't ship that path without a file conflict. Its copy is `/etc/xdg/nimbusos/kcm-about-distrorc`, and `/etc/xdg/plasma-workspace/env/10-nimbusos-xdg.sh`, which the Plasma session sources at login, puts `/etc/xdg/nimbusos` first in `XDG_CONFIG_DIRS`, idempotently. Every other default the spec names (`/etc/xdg/kdeglobals`, `kwinrc`, `plasmarc`, `ksplashrc`, `kscreenlockerrc`, `/etc/sddm.conf.d/`, `/etc/default/grub.d/`) has no owner in Trixie, so they ship where the spec says.
- **Prepended, never replaced:** the script sets `XDG_CONFIG_DIRS=/etc/xdg/nimbusos:<previous value>`, with `/etc/xdg` as the previous value when none is set, and does nothing if `/etc/xdg/nimbusos` is already in the list. The install test sources it twice and gets `/etc/xdg/nimbusos:/etc/xdg`.
- **Read by SDDM-started Wayland sessions (checked in Trixie's `plasma-workspace`):** `startplasma-wayland` and `startplasma-x11` look for `plasma-workspace/env/` in every `XDG_CONFIG_DIRS` directory and in `~/.config`, source each `*.sh` there through `/usr/lib/x86_64-linux-gnu/libexec/plasma-sourceenv.sh`, and then copy the resulting environment into the systemd user manager. So KDE apps started by the session and by systemd both see the value. `/etc/profile.d` is not used, because a Wayland session started by SDDM doesn't run a login shell.
- **Shown after login (deliverable 11 smoke test):** in a graphical session on the installed system, `systemctl --user show-environment | grep XDG_CONFIG_DIRS` prints `XDG_CONFIG_DIRS=/etc/xdg/nimbusos:/etc/xdg`, and kinfocenter's About this System page shows NimbusOS with its logo and website.

**NOTE-56 — Themes are selected through Debian's alternatives (decided 2026-11-17).** Checked against `desktop-base` 13.0.4 in Trixie:
- **One switch, `desktop-theme`:** desktop-base's design has one master alternative, `desktop-theme` (link `/usr/share/desktop-base/active-theme`; Debian's highest is ceratopsian at 50). Every other alternative (`desktop-background` at 70, `desktop-background.xml`, `desktop-lockscreen.xml`, `desktop-login-background`, `desktop-plasma5-wallpaper`, and `desktop-grub` with its `desktop-grub.sh` slave) has its top entry pointing *through* `active-theme/…`, so they all follow the switch.
- **What NimbusOS registers:** `nimbusos-branding` registers `/usr/share/desktop-base/nimbusos-theme` as `desktop-theme` at priority 80, and nothing else. A separate entry at 80 on each of the other alternatives would outrank `active-theme`, so `update-alternatives --set desktop-theme …` would stop changing the wallpaper and login background, which is the opposite of what an administrator expects.
- **Complete theme directory:** `nimbusos-theme/` has every path desktop-base registers or checks: `wallpaper` (link to Nimbus Sky, with `contents/images/1920x1080.svg` and `gnome-background.xml`), `lockscreen/gnome-background.xml`, `login/background.svg`, `grub/grub-16x9.png`, `grub-4x3.png`, `grub_background.sh`, and `plymouth` (link to the nimbusos Plymouth theme, as Debian's themes do). desktop-base registers its active-theme GRUB entries only if those GRUB files exist, so a desktop-base upgrade keeps working while NimbusOS is active (tested).
- **Behaviour:** an administrator's `update-alternatives --set` puts the alternative in manual mode, which package upgrades keep. prerm removes the entry on remove/deconfigure, and every link falls back to ceratopsian in auto mode. `nimbusos-branding` Depends on `desktop-base`, since its framework owns these links.
- **Readers use the links:** SDDM's `background=` is `/usr/share/images/desktop-base/login-background.svg`, the global theme's `Image=DebianTheme`, and the lock screen's `/usr/share/wallpapers/DebianTheme/`.
- **No `default.plymouth` alternative exists in Trixie,** so the earlier approach stays for Plymouth only. `plymouthd.defaults` names `Theme=ceratopsian` directly, not desktop-base's `debian-theme`, so the theme switch doesn't reach Plymouth. postinst runs `plymouth-set-default-theme nimbusos` only while `/etc/plymouth/plymouthd.conf` has no `Theme=` line or names the distribution default, so an administrator's choice is never replaced. postrm resets only if `nimbusos` is still the configured theme. Both rebuild the initramfs through the `update-initramfs` trigger.
- **No `update-grub` trigger exists in Trixie** (none of grub-common, grub2-common, grub-pc or grub-efi-amd64 declares one), so `update-grub` is called directly, guarded by `command -v` and an existing `/boot/grub/grub.cfg`. postinst calls it on configure, and postrm on remove (the GRUB theme and background change) and on purge (the `grub.d` drop-in goes).

**NOTE-57 — How the theme's pieces are made.**
- **Raster images:** rendered from SVG at package build time: the wallpaper at 1920×1080, 2560×1440 and 3840×2160 (545 kB at 4K), plus the GRUB background, logo and highlight, and the Plymouth images. The whole package is 1.27 MB.
- **GRUB fonts:** made from Inter with `grub-mkfont --name=…`, so the names in `theme.txt` match exactly; the test checks all three.
- **`/etc/xdg/kdeglobals`:** a header plus the colour groups of `NimbusDark.colors`, so new users get the colours without running any tool.
- **Panel:** `layout.js` uses only the calls Plasma 6.3's own default-panel template uses (`new Panel`, `gridUnit`, `screenGeometry`, `addWidget`, `writeConfig`). It doesn't set a floating property, because Plasma 6 panels float by default.
- **Login screen:** the SDDM theme declares `QtVersion=6` for the `sddm-greeter-qt6` in Trixie and uses only QtQuick, QtQuick.Controls and SDDM's context objects.
- **Installer:** Calamares (Qt 6 with SVG support) uses the logo as SVG directly.
- **CC-BY-SA-4.0 text:** in `debian/copyright` it is byte-identical to SPDX `text/CC-BY-SA-4.0.txt` at commit `31846481de7ea17d92f2f46fad76abe58b2e9bb0` (SHA-256 `cde7883b…4beba54966`), noted in the file.

### Found while packaging the Control Center (deliverable 5, part 3)

**NOTE-58 — Purging nimbus-control never deletes user data.**
- **Removed on purge** (the Control Center's own configuration and caches):
  - the regular files at the top of `/var/lib/nimbus-control` (`schema_version`, `settings.json`, `shares.json`, `stacks.json` and any later state file) and its `backups/`;
  - `/var/cache/nimbus-privd`, `/run/nimbus-control` and `/var/lib/nimbus-privd/package/`;
  - the include block in `smb.conf`, together with the blank line before it, so the file comes back byte-identical;
  - `/etc/samba/nimbus/`: dpkg removes the conffile `nimbus.conf`, postrm the generated `settings.conf`. Also `/etc/samba/nimbus-shares.conf`;
  - `/etc/avahi/services/smb.service`, but only if it carries privd's marker comment `Generated by nimbus-control` (deliverable 8 writes it);
  - the two `adduser.conf` keys (NOTE-09).
- **Kept:**
  - `stacks/` with app data and volumes, container-owned files included;
  - `/var/lib/nimbus-vms` (VM disks and ISOs);
  - `/mnt/user`, `/mnt/disks`, `/mnt/parity` and every share folder;
  - SMB users and their Samba passwords;
  - the managed `/etc/fstab` block;
  - `/etc/nimbus/snapraid.conf`. It is configuration, but without it the parity can't be used to recover a disk;
  - `/var/lib/nimbus-privd` (SMB user list, fstab backups);
  - the system accounts. sysusers.d never removes accounts, and files and SMB users stay owned by them.
- **What the user sees:** purge prints the kept paths that exist, with the SMB user names, and says that Samba no longer serves the NimbusOS shares.
- **Remove:** `remove` keeps everything, as Debian does for configuration, so Samba keeps serving the shares.

**NOTE-59 — State migrations.**
- **Where it runs:** `nimbus-migrate` is the `ExecStartPre=` of `nimbus-control.service`. It runs as the `nimbus-control` user inside the service's sandbox, so every start (the first after an upgrade included) migrates first, and a failure keeps the backend from starting on half-migrated state. It doesn't run in the postinst: there it would run as root, and in the image build chroot there is no state until first boot.
- **Steps:** `control-center/migrations/NNNN_name.py`, each with `DESCRIPTION` and `migrate(ctx)`, numbered without gaps and installed to `/usr/lib/nimbus-control/migrations/`. A step changes state files only through `ctx.read_json`, `ctx.write_json` and `ctx.remove`, which accept plain top-level `*.json` names.
- **A run:**
  1. Take a lock.
  2. Copy the state files to `backups/schema-<from>-<UTC time>/`. The last 5 copies are kept.
  3. Run each step, writing `schema_version` after it atomically (temporary file, fsync, rename).
  4. On any exception: restore every state file from the backup, log one line at priority err (identifier `nimbus-migrate`, with the step, schemas, backup path and full traceback as journal fields) and exit 1. Example: `state migration 0003 (break things) failed: OSError: [Errno 28] No space left on device. The state files were restored from /var/lib/nimbus-control/backups/schema-2-… (schema 2). nimbus-control will not start until this is fixed. …`
- **Refused without changing anything:** state newer than the newest step (a downgrade, exit 3), state files without `schema_version` (exit 3), and a gap in the step numbers (exit 2). `stacks/` is never read, written or backed up.
- **Schema 1 (`0001_initial`):** `settings.json` `{}`, `shares.json` `{"shares": []}`, and `stacks.json` `{"stacks": {}}`, where each stack records its catalog app id and the `template_version` it was created from (NOTE-60). Deliverable 8 reads and writes these.

**NOTE-60 — Template versions.**
- **The field:** every app manifest has `template_version`, a whole number of at least 1. It is raised whenever the app's compose template or its inputs change. Deliverable 9 adds it to `manifest.schema.json` and `SCHEMA.md`.
- **Build check:** `packages/nimbus-catalog/debian/check-catalog.py` runs at build time and enforces `template_version` independently of the schema. It also checks that the id equals the directory name, that the required files exist, that there are no symbolic links, and that each manifest passes the schema. One bad manifest fails the build.
- **Template update available:** an installed stack records in `stacks.json` the version it was created from. When the installed manifest's version is higher, the Control Center shows "Template update available". Applying the update is the user's action (deliverable 8).
- **No maintainer scripts:** `nimbus-catalog` has none, so installing, upgrading or purging it can't touch a stack (tested).

**NOTE-61 — nimbus-control's maintainer scripts and services, including the image build chroot.**
- **debhelper:** accounts come before directories. `dh_installsysusers` is called explicitly, because compat 13's sequence doesn't include it, and it runs before `dh_installtmpfiles`. Then `dh_installsystemd --no-start --no-stop-on-upgrade` enables the units but never starts or stops them. With `--no-start`, debhelper also leaves out the stop on removal, so the package's own prerm stops the units on `remove`.
- **Starting:** only through `deb-systemd-invoke`. It asks policy-rc.d first, and a 101 (live-build's chroot policy) makes it do nothing (checked in Trixie's init-system-helpers). On upgrades, nimbus-control is restarted only if the checksum of the package's files under `/usr/lib` changed; privd is then stopped (socket activation starts the new code) and netfix re-run. Otherwise the postinst only "starts", which changes nothing for a running unit and nothing for a unit the administrator disabled.
- **systemctl:** one call, on first install: `systemctl --no-reload disable nmbd.service`, which also works offline. lintian's warning is overridden with that reason. Trixie's samba, wsdd2, avahi-daemon and libvirt-daemon-system enable their own units on install (checked), so nimbus-control enables nothing else.
- **Forbidden commands:** no maintainer script calls docker, virsh or smbcontrol. The test checks this statically and with call-logging wrappers.
- **Units:** `nimbus-control.service` runs nimbus-migrate first and has the §4.3 hardening, with `ReadWritePaths=` `/var/lib/nimbus-control`, `/run/nimbus-control` and `-/var/lib/nimbus-vms/isos`. Directories come from tmpfiles.d, not `StateDirectory=`, which would chown recursively over container-owned files. The SnapRAID and recycle-bin timers are always enabled; the SnapRAID service has `ConditionPathExists=` for its config and binary.
- **What deliverable 8 must provide:**
  - the programs the units start: `nimbus-control-server`, `privd/nimbus-privd.py`, `nimbus-snapraid-run`, `nimbus-recycle-clean`, `nimbus-netfix` (which must exit 0 while Docker's `DOCKER-USER` chain doesn't exist yet);
  - their lines in `debian/install`;
  - the Avahi marker comment (NOTE-58).

### Found while writing the hooks (deliverable 6)

**NOTE-62 — The live-build hooks.** Checked against live-build `1:20250505+deb13u1`:
- **When they run:** `lb chroot_hooks` runs after the packages and `includes.chroot_after_packages` are in place, and while live-build's `policy-rc.d` still forbids starting services. It runs all of Debian's `config/hooks/normal/*` first: these empty `/etc/machine-id`, truncate logs, delete temporary files and remove `.pyc` caches. Only then does it run `config/hooks/live/*`. So NimbusOS's hooks run after that cleanup, and they must leave nothing behind themselves. `hooks.test.sh` checks this for hook 0100, and the verify hook checks the machine-id.
- **What hooks can read:** while hooks run, live-build bind-mounts the configuration read-only at `/live-build/config`. Hook 0300 reads the live-only units from there, and hook 0900 reads the package lists.
- **No hook 9990:** live-build's `chroot_hacks` runs `update-initramfs -k all -t -u` after all hooks (with `--initramfs live-boot`), so an initramfs hook of NimbusOS's own would be overwritten. Instead, `build.sh` checks the result: the ISO's initramfs must contain the nimbusos Plymouth theme and Plymouth's script plugin (`nimbus_check_image_initrd`, §2 step 6). Tested against real Trixie initramfs images.
- **0100-flathub-preseed:** runs `nimbusos-release`'s own Flathub setup (NOTE-11), which writes its stamp, so the boot-time unit skips itself. It retries twice, then fails the build: the image promises Flathub. `flatpak remote-add --system` works as root without D-Bus, so also in the chroot. The hook removes Flatpak's download cache.
- **0200-live-installer-launcher:** `calamares-settings-debian` 13.0.13 ships the menu entry `calamares-install-debian.desktop` ("Install Debian") and an autostart entry that copies it onto the desktop. The hook sets `NoDisplay=true` and `Hidden=true` in their `[Desktop Entry]` groups, in place. Calamares purges that package from installed systems, so a diversion would only leave a stale record on the target. If Debian renames either file, the build fails here. The live session shows "Install NimbusOS" instead (deliverable 7). Debian's own entry already has one desktop-file-validate error (`StartupNotify=True`); the hook adds none.
- **0300-live-units:** enables every unit file in `includes.chroot_after_packages/etc/systemd/system/` (`systemctl enable` works offline). A unit that didn't reach the chroot, or can't be enabled, fails the build. Deliverable 7's `var-lib-docker.mount` (NOTE-21) therefore needs an `[Install]` section, and `nimbus-target-cleanup` must remove it and its link from installed systems.
- **0900-verify-image:** 18 checks plus one warning (15 at deliverable 6; deliverable 7 added `installer`, `live-files` and `installer-launcher`, NOTE-63), each printed as ok, FAIL with the reason, or warn; any FAIL stops the build:
  - `/usr/lib/os-release` identity and its diversion (`/etc/os-release` stays diverted by live-build until after the hooks);
  - the seven NimbusOS packages at one version;
  - every package in the package lists installed, and every Depends and Recommends of `nimbusos-desktop`;
  - the groups new users and the Control Center need, and `nimbus-control`'s memberships;
  - the `adduser.conf` keys;
  - services: 18 units enabled, SDDM as the display manager, nmbd off;
  - the Plymouth, desktop-base and SDDM themes;
  - Flathub, the NimbusOS APT source with its keyring, and the pinning;
  - no unfilled build token in any NimbusOS file;
  - an empty machine-id.

  A live-user group from `NIMBUS_LIVE_USER_GROUPS` that doesn't exist only warns: live-config silently skips it, and `bluetooth`, `scanner` and `lpadmin` depend on optional packages. Deliverable 7 adds its own checks (Calamares settings and branding) to this hook.

### Found while writing the live files and boot menus (deliverable 7)

**NOTE-63 — The installer and the live-only files.** Checked against Calamares `3.3.14-1`, `calamares-settings-debian 13.0.13`, live-config and live-build in Trixie:
- **Sequence:** Debian's own, with branding `nimbusos`. Debian's `sources-final` is replaced by `shellprocess@nimbus-sources-final`, and `shellprocess@nimbus-target-cleanup` runs after it, before `umount`. Both run inside the installed system (`dontChroot: false`), where their scripts arrived with the copied image. That needs no shellprocess variables: Calamares 3.3 keeps those names as UTF-16 literals, so they couldn't be confirmed from the binary.
- **Module settings:** Debian's `calamares` package ships no default module configs. So modules without a file in `/etc/calamares/modules` run on built-in defaults; `plymouthcfg`, for example, then leaves the NimbusOS boot splash alone. The files NimbusOS changes:
  - `users.conf`: Debian's groups plus `docker`, `libvirt`, `kvm`, `nimbus-control` with `must_exist: true`. A missing group stops the install with Calamares' "These groups are missing in the target system" instead of producing a user without Control Center access. Debian's optional groups are created if missing, as Debian does. The host name template is `nimbusos`.
  - `bootloader.conf`: `efiBootloaderId: nimbusos`, equal to the lower-cased `GRUB_DISTRIBUTOR` (NOTE-07). Calamares' `grubcfg` writes `GRUB_DISTRIBUTOR` from the branding's `bootloaderEntryName` ("NimbusOS"), and `/etc/default/grub.d/10-nimbusos.cfg` sets the same value.
  - `packages.conf`: Debian's removal list plus `calamares` (apt `--purge remove`, then `autoremove`).
  - `displaymanager.conf`: SDDM only. Automatic login uses `plasma`, which Calamares finds in `/usr/share/wayland-sessions/` (NOTE-08).
  - `welcome.conf`: at least 32 GB of disk and 4 GB of memory. This is a policy choice, to be checked against the installed size in deliverable 11.
- **Live autologin:** live-config's `0085-sddm` writes `Session=plasma.desktop` when the session manager is Plasma. In Plasma 6 that file is the Wayland session, so the live session needs no NimbusOS file. The deliverable 11 smoke test confirms it.
- **`sources-final`:** writes `/etc/apt/sources.list.d/debian.sources` (deb822) for trixie, trixie-updates and trixie-security, with `main contrib non-free non-free-firmware`, Signed-By `/usr/share/keyrings/debian-archive-keyring.pgp`, and no backports. `sources.list` keeps only comments. Tested with `apt-get update` against deb.debian.org: no warning, and all 12 suite/component pairs are fetched. Some are empty today (no contrib update yet), and apt keeps no file for those.
- **Removing live-only files from installed systems:** hook 0400 writes `/usr/lib/nimbusos-live/live-files` from the image's `includes.chroot_after_packages` itself, so the list can't drift. It fails the build if a live file belongs to a package that stays installed. `target-cleanup` then disables each listed unit, removes each listed file (but keeps one owned by an installed package, and says so) and removes `/usr/lib/nimbusos-live`. Both tmpfs mounts also have `ConditionKernelCommandLine=boot=live` as a second line of defence.
- **Launchers:** "Install NimbusOS" (`nimbus-install.desktop`) runs Debian's own wrapper `calamares-install-debian` (pkexec, its fstab and Xwayland handling). `live-session-setup` copies it onto the live user's desktop, executable, once, and only when `/run/live/medium` exists.

**NOTE-64 — The live boot menus.** Checked against live-build `1:20250505+deb13u1`; tested with its own `binary_grub_cfg`, `binary_syslinux` and `binary_bootloader_splash`:
- **GRUB (UEFI):** live-build names its generated entries "Live system (amd64)" in code, not in a template. So `grub-pc/grub.cfg` has NimbusOS's own three entries, built from live-build's kernel, initrd and boot-option tokens, as its template suggests for extra entries:
  - "Start NimbusOS";
  - "Start NimbusOS (safe graphics)", the same with `nomodeset`;
  - "Start NimbusOS (fail-safe mode)";
  - plus live-build's Utilities submenu.

  The menu has a 10-second timeout. `theme.cfg` loads the three Inter fonts and the nimbusos theme from `live-theme/` (GRUB doesn't load a theme's fonts by itself). live-build rewrites bare token names and `//` in every line, so these files' comments avoid both.
- **ISOLINUX (BIOS):** `syslinux_common/live.cfg.in` has the same three entries, `menu.cfg` the title, `stdmenu.cfg` the Nimbus Dark colours, and `isolinux/isolinux.cfg` a 10-second timeout (live-build waits forever by default).
- **Fail-safe options:** live-build's default fail-safe line drops `username`, `user-default-groups`, the locale and the rest, which would leave the live user without the Control Center groups. `build.sh` therefore passes `--bootappend-live-failsafe`: the same live settings plus live-build's own fail-safe options (`memtest noapic noapm nodma nomce nosmp nosplash vga=788`).
- **Splash:** `splash.svg` uses the logo and palette, and live-build's `@VERSION@` token is filled from os-release. live-build renders it at 640×480 for ISOLINUX and 800×600 for GRUB's fallback. The title stays above text row 12, where the ISOLINUX menu starts.
- **Theme preparation:** it moved from `build.sh` into `nimbus_prepare_grub_theme` (`build/lib/common.sh`), so the test runs exactly the build's code.

### Found while building the Control Center (deliverable 8)

**NOTE-65 — Control Center, batch 1 of 5: the foundation and the Updates page.** Deliverable 8 comes in five vertical batches, each complete and tested:
1. this foundation plus Updates;
2. Apps (Docker);
3. Virtual machines;
4. Storage, Shares and Settings;
5. Dashboard and metrics, which need the other four.

Until batch 5, the router shows a clear "could not be shown" state for a page whose module isn't there yet. That is its normal error handling, not a stand-in page.
- **Install layout:** Python packages go in `/usr/lib/nimbus-control/python/`, put on the module path by the two entry points (`bin/nimbus-control-server`, `privd/nimbus-privd.py`). The frontend goes in `www/`. `nimbus-control` now Depends on `nimbusos-branding` (its logo is the window's logo, the launcher's icon and the favicon) and on `kdialog` (the launcher's messages).
- **privd protocol** (`nimbus_common/protocol.py`): one request per connection, as JSON lines: `{id, verb, args}`, then any number of `{event: "log"}` lines, then `{ok, result}` or `{ok: false, error: {code, message, field}}`.
  - Error codes: `invalid_request`, `unknown_verb`, `invalid_argument`, `denied`, `busy`, `failed`, `internal`.
  - The backend maps them to HTTP 422, 409, 502 and 503 (`unavailable`, the socket can't be reached), else 500, with a message the user can act on.
- **privd checks:** `SO_PEERCRED` must show the `nimbus-control` uid, so a desktop user in the group can open the socket but is refused (tested on the installed package). Then come the verb allowlist (`registry.py`) and per-argument validators from `nimbus_common.validate`, which reject undeclared arguments. A verb's lock makes a second caller "busy". Commands are absolute-path argument lists in a minimal environment.
- **Audit:** one journal entry per request and outcome, with structured `NIMBUS_*` fields and secrets as `***`. The same line also goes to stderr unless stderr already is the journal (`JOURNAL_STREAM`): without journald, `journal.send()` drops entries silently, which the installed-system test caught.
- **Updates through nimbus-update:** `status`, `check`, `channel`, `policy` and `history` call `nimbus-update --json …` and return its document even when it exits non-zero. `check` exits 1 when a repository failed, and the document carries the classified error. The installed-system test found this: a first version turned it into a generic "failed", hiding the fix the user needs.
- **Installing updates:** `install` starts `nimbus-update install` as the transient unit `nimbus-update-<job>.service` (`systemd-run`, `RemainAfterExit=yes`) and returns the job. `follow` streams that unit's journal until it has finished, then reports the exit status. The page reconnects (and shows the whole log again) if the backend itself restarts during an upgrade (NOTE-35).
- **Authentication:**
  - The token is 43 url-safe characters in `/run/nimbus-control/token` (0640, group `nimbus-control`), kept across service restarts and new every boot.
  - Launch codes: 30 seconds, single use, at most 16 pending.
  - WebSockets offer the subprotocols `nimbus.v1` and `nimbus.token.<token>`, and the server echoes `nimbus.v1`.
  - The CSP is `default-src 'self'` with explicit `ws://127.0.0.1:7777` and `ws://localhost:7777`, `frame-ancestors 'self'` and `object-src 'none'`.
  - No OpenAPI or docs pages are served.
- **Grant access** (`nimbus-grant-access`, polkit `auth_admin`): adds only the user who ran pkexec (`PKEXEC_UID`), and only a regular account (uid 1000 and up), to `NIMBUS_USER_EXTRA_GROUPS`. No user name is ever taken from the command line.
- **Fonts:** static Inter (400, 500, 600, 700) and JetBrains Mono (400, 500), found through `dpkg-query -L` (NOTE-13). The spec's 650-weight headings render with the 700 face by CSS weight matching, since Debian ships no variable Inter.
- **What deliverable 10 must provide:** `releases.json` entries need `version`, `deb_version`, `channel`, plus `date`, `notes` and `restart_required`, which the Updates page shows.
- **Spec settings carried to batch 4:** the spec also lists `smb encrypt = if_required` and global `vfs objects = catia fruit streams_xattr`. They interact with per-share VFS modules (recycle bin, Time Machine), so batch 4 adds them to `nimbus.conf` together with the share renderer and checks them with `testparm`.

**NOTE-66 — Control Center, batch 2 of 5: Apps.** The Apps page has an Installed list, the Catalog, 1-click install and Advanced install with a Compose tab. The backend is `manifest.py`, `catalog.py`, `compose.py`, `apps_service.py`, `jobs.py`, `docker_api.py` and `state.py`, plus privd's `stacks.py`. No new lines in `debian/install` were needed: everything sits in folders batch 1 already installs.
- **The manifest contract (for deliverable 9, which added `webui.scheme`, `single_instance`, `companions` and stricter checks: NOTE-70).** `manifest.py` defines what an app may declare:
  - `id` (equal to its folder), `name`, `description`, `category`, `template_version` ≥ 1 and `main_service`;
  - `network_mode` (`bridge` or `host`);
  - `ports[]`: `id`, `container`, `host`, `protocol`, `bind` (`all` or `lan`), `fixed`;
  - `volumes[]`: `id`, `container`, `kind` (`bind` or `named`), `default`, `share_hint`, `owner {uid, gid}`;
  - `env[]`: `name`, `default`, `required`, `secret`, `generate` (`password`, `hex` or `base64`);
  - `host_paths[]`, `webui {port, path, secure_context}` and `notes`.
  - `manifest.schema.json` and `SCHEMA.md` must describe the same contract. The catalog applies the schema when `python3-jsonschema` loads it, and `parse_manifest` always. A broken app is listed on the Catalog tab; the others still work. A changed file is picked up on the next request (mtime and size), so a `nimbus-catalog` upgrade needs no restart.
- **The deploy engine.** The template is rendered in the sandbox (NOTE-30) and sees `stack`, `env`, `ports` (port id → host port), `host_ip` and `hostname`. The engine then owns the main service's ports, volumes (plus `host_paths`), environment, restart policy and network mode, and the top-level `name:`. Template values come first, then the install settings, sorted and with `$` doubled. Sorting makes the same settings give the same file byte for byte, so a template diff shows only real changes.
  - Named volumes stay project-scoped (`<stack>_<volume>`), so two installs of one app never share a database.
  - A template that needs one fixed volume name (Nextcloud AIO, NOTE-29) declares it with `name:` at the top level, and the engine keeps that.
- **1-click defaults:**
  - **Host ports:** the first free port from the manifest's, skipping Docker's published ports, listening sockets, other apps' ports and installs still running. A `fixed` port never moves: the plan warns, and the install then stops at that field.
  - **LAN binding:** `bind: lan` uses the default route's address (NOTE-18).
  - **Folders:** `share_hint` uses that share's folder when it exists, else the stack folder.
  - **Secrets:** generated (`password`: 32 URL-safe characters; `hex`: 64; `base64`: 44).
  - **Restart policy:** `unless-stopped`.
  - **Names:** `<app>`, then `<app>-2`, and so on.
- **Validation.** Every error names its input (`name`, `ports.0.host`, `volumes.1.source`, `env.NAME`, `env.NAME.name`), and the Advanced form highlights that field.
  - A host port in use names its holder and suggests the next free port.
  - A volume source must be one of: a folder in the app's stack folder or in a share (resolved with `realpath`, so links can't lead out), a relative path inside the stack folder, or a Docker volume name.
  - The bind rules (NOTE-30) apply to every deploy, generated or hand-edited, in every service and in both volume syntaxes.
  - `docker compose config` must then accept the file before anything is written.
- **Jobs.** Install, edit, start, stop, restart, update (pull and up), template update and uninstall each run as a job. Only one job runs per app (otherwise 409). The log streams over `/ws/jobs/<id>`, and finished jobs are kept for an hour.
  - The app's record in `stacks.json` is saved as soon as its Compose file is written. If the pull then fails (no network), the app stays listed with its error and can be started again or uninstalled.
  - A file that `docker compose config` rejects leaves nothing behind, not even an empty folder.
- **Status.** Containers are matched to apps by the Compose project label. What the user last asked for (`desired`: running or stopped) decides what counts as an error:
  - a container that stops while its app should run is an error, with the reason: exit code, out of memory, restarting, unhealthy, or containers missing;
  - a container the user stopped is not.
  - If Docker is down, every app shows "unknown" and a banner says how to start Docker; the page still works.
- **Data and uninstall (NOTE-31).**
  - `stack.prepare_volume` makes a folder and hands that one folder to the container's uid; it is not recursive.
  - `stack.delete_data` deletes `/var/lib/nimbus-control/stacks/<stack>`.
  - Both walk from a handle on the stacks folder with `O_NOFOLLOW`, and `rmtree` runs only where it is symlink-safe. A link planted in a stack, a stack that is a link, and the stacks folder swapped for a link are all refused (tested).
  - Uninstall keeps the data folder and named volumes. "Delete data" also passes `--volumes` and asks a second time, with the name typed.
- **Template updates (NOTE-60).** "Template update" appears when the catalog's `template_version` is newer than the app's. The diff (the installed file against the new template with the app's own settings) is shown first, with secret values masked. Nothing changes until the user applies it. If the file was edited by hand, the dialog says the template replaces those edits.
- **Web page link:** `http://<hostname>:<port><path>`, or `http://localhost:<port>` when the app needs a secure context (NOTE-29).
- **Live logs:** `docker compose logs --follow --tail 200`, stopped as soon as the window closes, not at the next log line.
- **What batch 4 must provide:** `shares.json` entries need `name` and `path`. Other fields are ignored here. `share_hint` matches `name`.

**NOTE-67 — Control Center, batch 3 of 5: Virtual Machines.** The page has a Machines list, an ISO library, a Create VM wizard, VM options and an embedded console. The backend is `libvirt_api.py`, `vm_xml.py`, `vm_service.py` and `iso_library.py`. The frontend is `views/vms.js` and `lib/vnc.js`, plus `console.html` and `console.js` for the pop-out window. Checked against Trixie's libvirt 11.3.0, QEMU 10.0.13, OVMF 2025.02, swtpm 0.7.1, noVNC 1.6.0 and websockify 0.12.0.
- **No root, no privd.** `nimbus-control` reaches `qemu:///system` as a member of the `libvirt` group: `libvirtd.socket` is 0666, and Debian's polkit rule `60-libvirt.rules` allows that group. VMs, their disks and the disk pool are therefore made by libvirtd itself, and this batch adds no privd verbs.
  - The `nimbus-images` pool (a directory pool on `/var/lib/nimbus-vms/images`) is defined, set to autostart and started on first use, through the libvirt API. No maintainer script is involved.
  - ISO uploads are written by the backend itself: `/var/lib/nimbus-vms/isos` is `root:nimbus-smb` 2775, the service user is in `nimbus-smb`, and the folder is in its `ReadWritePaths=`.
- **Domain XML is built with ElementTree** (`vm_xml.py`), so every value is escaped by the XML writer. Both presets use the q35 machine.
  - **UEFI** uses libvirt's firmware autoselection (`<os firmware="efi">`) from OVMF's descriptors in `/usr/share/qemu/firmware`. Linux turns Secure Boot off, so libvirt picks `OVMF_CODE_4M.fd`. Windows turns it on with enrolled keys, so libvirt picks `OVMF_CODE_4M.ms.fd` with Microsoft's keys, and SMM is on. The test against real libvirt showed the choice is made when the VM is defined.
  - **Windows** also gets TPM 2.0 through swtpm (`tpm-crb` with UEFI, `tpm-tis` with BIOS) and the Hyper-V enlightenments relaxed, vapic, spinlocks (8191), vpindex, runtime, synic, stimer, reset, frequencies, tlbflush and ipi. Its clock is local time with `hypervclock`.
  - **Linux** gets virtio disk, network, graphics and RNG, and a UTC clock.
  - **Always:** the disk boots first and the CD second, so an installed system boots from its disk with the ISO still in the drive. VNC listens on 127.0.0.1 with a random 8-character password (NOTE-16). A USB tablet keeps the console pointer aligned. There is a memory balloon with statistics, a guest-agent channel, and the preset in metadata under `urn:nimbusos:vm:1`.
  - libvirt's own schema (`virt-xml-validate`) accepts all 24 combinations of preset, firmware, driver ISO, network and KVM or TCG.
- **Deviation: Windows without the virtio-win driver ISO.** The spec asks for virtio drivers in the Windows preset, but Windows Setup can't see a virtio disk without them, and Debian doesn't package virtio-win.
  - With a driver ISO (from Fedora's virtio-win), the VM uses a virtio disk and network, and the ISO sits in a second CD drive for Setup's "Load driver".
  - Without one, the VM uses SATA and e1000e, which Setup finds on its own. The wizard says which applies and suggests a `*virtio-win*` ISO when the library has one.
- **No hardware virtualization.** When libvirt reports no KVM (VT-x or AMD-V off in the firmware, or no `/dev/kvm`), VMs are defined as TCG domains without `host-passthrough`. They are very slow, and a banner says how to turn KVM on. This is also how the real-libvirt test runs in a container.
- **Create VM** checks every setting and names the field:
  - name, preset, vCPUs (up to the host's threads), memory (256 MiB up to the host's RAM) and disk (1 GiB to 64 TiB, thin qcow2);
  - firmware (UEFI only with OVMF present);
  - NAT (needs libvirt's `default` network) or a bridge that exists on the host (not `virbr*`, `docker*` or `br-*`);
  - ISOs from the library.
  - If libvirt refuses the domain, the new disk is deleted again. A VM that doesn't start is kept, and the message says why.
  - A disk name never reuses a disk kept from a deleted VM: `<name>.qcow2`, then `<name>-2.qcow2`, and so on.
- **The list** shows every VM on `qemu:///system`, including ones made with virt-manager. Each shows its state (paused VMs with the reason, for example a disk error), its CD, and live CPU (as % of its own vCPUs and of the host) and memory (`balloon.rss`), from successive `getAllDomainStats()` samples (§4.4). Batch 5's collector reuses `LibvirtApi.stats()`.
- **Actions:** Start, Shut down (ACPI; the guest decides, and the message says to use Force off if it doesn't react), Restart, Force off (with a confirmation), Resume, and Options (change or eject the CD while running; start with the computer).
- **Delete** turns the VM off and removes it with its UEFI variables, TPM state and saved state. The disk is deleted only when ticked and the name is typed, and only disks in the `nimbus-images` pool; others are listed as kept.
- **Console:**
  - `POST /api/vms/<name>/console` writes one websockify token file per VM to `/run/nimbus-control/vnc-tokens/<uuid>`: `<token>: <host>:<port>`, written atomically, mode 0640. It returns the WebSocket path and the VNC password.
  - A new console replaces the VM's previous token. Tokens of VMs that stopped are removed when the list refreshes, and all tokens at service start.
  - websockify (`TokenFile`, which re-reads the folder on every connection) refuses unknown tokens. The VNC password is a second lock.
  - The CSP's `connect-src` now also allows `ws://127.0.0.1:6080` and `ws://localhost:6080`.
  - The packaged noVNC is served from `/usr/share/novnc` at `/novnc/`. `lib/vnc.js` loads `/novnc/core/rfb.js`. The console opens in a large modal, with Ctrl+Alt+Del, full screen and pop out.
  - The pop-out `console.html` inherits the session, because `sessionStorage` is copied into windows a page opens, so the window is opened without `noopener`.
- **ISO uploads:**
  - Uploads come in 8 MiB chunks, at most 16 MiB per request and in order, with progress. Closing the drawer cancels.
  - The limit is 32 GiB per file (`NIMBUS_CONTROL_ISO_MAX_BYTES`). An upload is refused when the free space is less than its size plus 256 MiB.
  - Every file is opened relative to a handle on the folder with `O_NOFOLLOW`, and names are plain file names. A link planted in the folder (it is writable over SMB) is never followed, listed or deleted through. A part file swapped for a link stops the upload.
  - Finishing uses a hard link, so it never replaces a file that appeared meanwhile; `rename` is the fallback where hard links don't exist. Finished files are 0644 so QEMU can read them.
  - Unfinished uploads are removed after an hour without data, and at service start.
  - An ISO in a VM's CD drive can't be deleted until it is ejected.
- **What batch 4 must provide:** the `isos` share should create files readable by others (0644), so QEMU (`libvirt-qemu`) can read ISOs copied in over SMB.

**NOTE-68 — Control Center, batch 4 of 5: Storage, Shares and Settings.** The Storage & Shares page has four tabs (Shares, Disks, Pool, Users) and the "Set up NAS" wizard; Settings has network sharing, alert thresholds and the firewall note. Checked against Trixie's Samba 4.22.11, mergerfs 2.40.2, SnapRAID 12.4, util-linux 2.41, parted 3.6, xfsprogs 6.13, smartmontools 7.4 and adduser 3.152.
- **Where the code is.**
  - The privd verbs are in `storage.py` (`disk.list`, `disk.smart`, `disk.format`, `disk.mount`, `disk.unmount`, `pool.apply`, `snapraid.status`, `snapraid.sync_now`) and `samba.py` (`share.apply`, `share.delete_data`, `samba.settings`, `samba.test`, `smbuser.add`, `.passwd`, `.delete`, `.list`), with `fsutil.py` for atomic files and the fstab block.
  - The backend is in `storage_service.py` and `shares_service.py`.
  - What a share is, and where its folder is, lives in `nimbus_common/shares.py`, so the backend, privd and the recycle-bin timer use one definition. privd computes every path from a share's name and location and never takes a path as input.
  - The spec's example verb `samba.reload` is not a separate verb: `share.apply` reloads after its checks, so nothing can reload an unchecked configuration.
- **Disks.**
  - lsblk's JSON tree is walked through partitions, LUKS and LVM. A disk with anything mounted at `/`, `/boot*`, `/usr`, `/var`, `/home`, `/srv`, `/opt` or as swap is the system disk; one with `/run/live/*` is the live medium. Neither can be formatted, and neither can a disk with anything mounted.
  - Formatting asks for the serial number to be typed. A disk that reports none (a VM's virtual disk) asks for its name instead.
  - Labels are at most 12 characters, XFS's limit. ext4 keeps 1 % reserved on data disks and 0 % on parity disks.
  - SMART is cached for an hour and read with `--nocheck=standby`. A sleeping disk keeps its last reading and shows as asleep.
  - "Add" mounts an existing XFS, ext4 or btrfs filesystem, for example after a reinstall. "Unmount" is refused while the disk is in the pool or the parity set, or holds shares.
- **Real formatting is not run in containers.** Rootless containers can't make loop devices, and a privileged container sees the host's disks, so `disk.format` and `disk.mount` are tested with every command replaced. They run on real disks in deliverable 11's QEMU test, which has three blank virtio disks.
- **fstab.** The managed block holds the disks by `UUID=` with `nofail,x-systemd.device-timeout=30s` (fsck pass 2 for ext4), the mergerfs line and the ISO bind mount. The rest of the file is kept byte for byte, a timestamped backup is taken first (the last 10 kept), and systemd reloads its mount units.
- **The pool.**
  - mergerfs options: `allow_other,cache.files=off,dropcacheonclose=true,category.create=mfs|epmfs,moveonenospc=true,fsname=nimbus-pool`, with `x-systemd.requires-mounts-for` for each disk.
  - On a mounted pool, disks and the create policy change in place through `/mnt/user/.mergerfs`, without unmounting.
  - Found while testing: mergerfs creates nothing on a disk with less than 4 GiB free (its `minfreespace` default). Pooled disks are expected to be larger than that.
- **Parity.**
  - `/etc/nimbus/snapraid.conf` has one or two parity disks (each at least as large as the largest data disk), a content file in `/var/lib/nimbus-privd` plus one on each data disk, and excludes `.recycle/`.
  - `nimbus-snapraid-run` syncs nightly, scrubs 8 % of blocks older than 10 days once a week after a good sync, and records the result.
  - `snapraid.status` adds `snapraid diff` counts, cached 10 minutes. (This said the alert would use the file count; batch 5 added the bytes, so the 10 GB rule is the spec's, NOTE-69.)
  - Turning parity off keeps the old configuration in `/var/lib/nimbus-privd`.
- **Shares.**
  - Folders are `root:nimbus-smb` 2775, made link-safe. Each share forces group `nimbus-smb`, create mode 0664 and directory mode 2775, so ISOs copied into the `isos` share are readable by QEMU (NOTE-67).
  - **Security:** Public is guest read and write. Secure is guest read, with a write list. Private has valid users, a write list and a read list; with nobody chosen it is unavailable.
  - **Options:** recycle bin (`vfs_recycle` into `.recycle`, deletion time as mtime), Time Machine with a size cap, hosts allowed (plus 127.0.0.1, for the Test button). A share's location can't change later.
  - **Missing disk:** a share whose disk isn't mounted is published as unavailable, and nothing is created on the system disk in its place.
  - **The isos share** is bind-mounted onto `/var/lib/nimbus-vms/isos` (NOTE-27), and the ISOs already there move into it.
  - **Deleting** a share removes the export; the folder goes only when that is ticked and the name typed.
- **Checking Samba's configuration.** `testparm` exits 0 even with unknown parameters, so privd reads its messages.
  - It checks a candidate copy of the whole configuration (smb.conf → nimbus.conf → the new files) first.
  - Then it swaps the files in atomically, reloads smbd, checks the live configuration again, and restores the old files on any failure.
  - NOTE-23's fallback puts the include back if smb.conf lost it.
- **Found by the real testparm.** Debian's smb.conf defines `[homes]`, `[printers]` and `[print$]` before NimbusOS's include, so those sections missed the later defaults. Samba warned that "some services use vfs_fruit, others don't", which macOS handles badly.
  - The packaged `nimbus.conf` now gives `[homes]` the same encryption and macOS settings as every share (Debian's home shares stay as they are), and turns the two printer shares off.
  - It also adds the spec's `smb encrypt = if_required`, `vfs objects = catia fruit streams_xattr` and the fruit settings that were moved to this batch (NOTE-65).
- **SMB users.**
  - New users are system accounts in `nimbus-smb` (`adduser --system`, no login, no home). Because `adduser --system` returns 0 for a name that exists, privd checks with getent first.
  - A desktop account (uid 1000 and up) only joins `nimbus-smb`. This uses `gpasswd`, because adduser 3.152's `deluser USER nimbus-smb` rejects the hyphenated group name. Other system accounts are refused.
  - privd keeps the list of accounts it made in `/var/lib/nimbus-privd/smbusers.json`. If setting the password fails, the new account is removed again.
  - Passwords go only to `smbpasswd -s` on stdin, and to "Test connection" through a 0600 file. They are never on a command line, in a log or in the state.
  - Deleting a user removes them from every share first.
- **Settings.** `settings.json` holds Samba (on/off, workgroup, description, NetBIOS) and the temperature limits (50 °C for disks and SSDs, 70 °C for NVMe). Samba changes go through `share.apply` and `samba.settings`, which enables or disables `smbd`, `wsdd2` and, with NetBIOS, `nmbd`. The firewall note is NOTE-22's.
- **The wizard** runs as one job: format the new disks (XFS; `disk1…`, `parity1…`), set the mode, add the first SMB user (or give an existing one a new password), create the chosen shares, and turn Samba on.
  - The default shares are `media` and `isos` (Secure), and `documents`, `backups` and `timemachine` (Private, with Time Machine on for `timemachine`).
  - The done screen shows the connection strings and a Test connection button.
- **The live session:** privd and the backend both refuse formatting, the pool and the wizard, and the page says to install NimbusOS first.
- **Gap from deliverable 5, fixed here.** `nimbus-netfix.service` was enabled without its script, and the two NAS timers pointed at scripts that didn't exist. `bin/nimbus-netfix`, `nimbus-snapraid-run` and `nimbus-recycle-clean` now exist and are installed.
  - `nimbus-netfix` accepts libvirt's NAT traffic and frames inside the user's own bridges in Docker's `DOCKER-USER` chain. It adds each rule once, and does nothing without Docker.
  - `nimbus-control` now also Depends on `adduser`, `passwd` and `udev`.

**NOTE-69 — Control Center, batch 5 of 5: Dashboard and metrics.** The Dashboard is the landing page. It has a header with the hostname and a Live indicator; four cards (CPU, Memory, Network, Storage); "Where your resources go"; "Needs attention"; the Docker apps and VMs panels; and Quick open. Checked against Docker 29.10 (API 1.57), libvirt 11.3.0 and SnapRAID 12.4.
- **Where the code is.**
  - The backend is `metrics_service.py` (the collector and its two routes), `docker_stream.py` (Docker's streaming API) and `alerts.py` (the rules).
  - The frontend is `views/dashboard.js` and `lib/charts.js` (sparklines and stacked bars as inline SVG, no library).
  - The VM console dialog moved from `views/vms.js` to `lib/vnc.js`, so the Dashboard opens the same one.
  - No new lines in `debian/install` were needed: everything sits in folders batch 1 already installs.
- **Host numbers.**
  - psutil's `cpu_percent()` keeps one global "since the last call" state, so the collector and the sidebar's `/api/system` would shorten each other's intervals. The collector computes CPU % from its own `cpu_times()` deltas instead, and subtracts `guest` and `guest_nice`, which Linux already counts in `user` and `nice`.
  - Network is the default-route interface, read from `/proc/net/route`.
  - Temperature tries `coretemp`, `k10temp`, `zenpower`, `cpu_thermal` and `acpitz`, in that order. With none, the card leaves it out.
- **Docker.**
  - `docker_stream.py` speaks HTTP/1.1 to the Unix socket with asyncio, handling chunked and plain bodies, one JSON document per line. Each reader is a task that is cancelled when its container goes away: no thread per container and no `docker stats` subprocess.
  - CPU is `cpu_delta / system_delta × online_cpus × 100` (Docker's own figure, 100 % per core), divided by `online_cpus` for a share of the host.
  - Memory is `usage − inactive_file` (`total_inactive_file` on cgroup v1).
  - Readers are reconciled with the container list every 10 s. `/events` (die, oom, health_status, restart, start, stop, destroy, create) wakes the collector at once, with a 0.3 s debounce.
  - A restart history per app (3 in 5 minutes) gives the restart-loop alert.
- **VMs.**
  - One `getAllDomainStats()` call per sample, which includes stopped VMs.
  - CPU is shown both as a share of the host and as % of the VM's own vCPUs. Memory is `balloon.rss`, or the configured memory without it.
  - The timestamp for the CPU delta is taken at the call, not at the start of the sample.
- **Storage.**
  - `statvfs` of the pool and the system disk, every sample.
  - SMART and parity come from a copy. `refresh_storage()` fetches privd's `disk.list` and `snapraid.status` 60 s after start and then hourly; both answer from privd's caches, and SMART is read with `--nocheck=standby`. The Storage page's overview also hands its reading over.
  - So opening the Dashboard never runs smartctl or snapraid.
- **Found while testing: two samples at once.** The first `GET /api/dashboard` sampled inline while the loop's first sample was still running. The two shared the CPU-time baselines, and a VM showed 117 % of the host.
  - Samples now run under one lock, and the GET samples only if nothing has yet, inside that lock.
  - `test_samples_never_overlap` covers it.
- **Found while testing: libvirt's paused reasons.** Batch 3's table called reason 10 "disk space ran out" and 13 "a crash". In libvirt's `virDomainPausedReason`, 10 is a guest crash and 13 is a failed post-copy migration; 4 (dumping) was missing.
  - The table is corrected, and `state_of()` maps state and reason together; shut off with reason 3 also counts as crashed.
  - `test_vm_libvirt.py` now pauses a real VM and reads "paused by the user" (reason 1) back.
- **Found by the real SnapRAID: unprotected bytes.** NOTE-68 counted changed files, but the spec's alert is "more than 10 GB unprotected". `snapraid.status` now also sums the sizes of the files `snapraid diff` lists as added, updated, restored, moved, copied or relocated.
  - The first parser assumed lines like `add disk1/media/x`. A real diff names the file by its path inside its disk, shell-escaped: `add media/my\ film\ \(1\).mkv`. So it always found 0 bytes, which `nas-integration.sh` caught.
  - privd now unescapes the path and looks it up on the data disks. Links count as themselves; directories, `..` and absolute paths are skipped.
  - The `old -> new` form of move, copy and relocate lines comes from SnapRAID's source (`cmdline/scan.c`). A container can't produce those lines: without disk UUIDs, SnapRAID reports a move as remove + add. Deliverable 11's QEMU test, with real disks, checks them.
- **Alerts.** Each alert has a stable id, a severity (bad, warn, info), a title, an optional detail and one action. The actions use the same APIs as the tabs:
  - **Fix** opens the app's Edit dialog at the setting `GET /api/apps/<name>/diagnose` names: a host port that another app or another program holds (found with psutil), a volume folder that isn't there, or an empty required variable. Otherwise a toast gives the reason and points to Logs.
  - **Start** or **Open VMs**, **Open disks**, **Sync now**, **Install updates**, **See what's new**.
  - "Exited unexpectedly" means the stack's desired state is "running", so stopping an app is not an alert. Nothing is raised for an app while a job works on it.
  - Beyond the spec: the system disk over 90 % full, a failed `wsdd2` unit, NVMe media errors, a set-up disk that is missing, and a failed last parity sync.
- **Delivery.**
  - `GET /api/dashboard` returns the latest sample with an hour of history.
  - `WS /ws/metrics` pushes each sample. Each client has a queue of 3, so a slow window skips samples and never holds up the collector.
  - Sampling stops 10 s after the last client or GET.
  - The Live indicator shows when the socket is down; the page reconnects with backoff (1, 2, 4 … 30 s) and fetches the snapshot again.
- **Layout: a deviation.** "Needs attention" sits beside "Where your resources go", near the top, not at the bottom of the page: alerts are what the user acts on first. At 800 px it stacks under the split.
- **What could and couldn't be checked here.** Rootless Podman gives dockerd no cgroups, so no container can run in the dev container.
  - `docker-streams.sh` checks the protocol against a real Docker 29.10: events, the stats stream, its fields, and Docker's error message.
  - The CPU and memory formulas are checked with fixtures shaped like Docker's documents.
  - Real numbers are compared with `docker stats` and `virsh domstats` in deliverable 11's QEMU test.

### Found while writing the catalog (deliverable 9)

**NOTE-70 — The app catalog.** The ten apps the spec names, each a folder in `catalog/` with `manifest.json`, `docker-compose.yml.j2` and `icon.svg`. `catalog/SCHEMA.md` documents the format field by field, and `catalog/manifest.schema.json` (JSON Schema 2020-12) checks it. Every setting comes from the vendor's own install guide or README (read 2026-11-23), or from the image itself: `skopeo inspect` gives its user, exposed ports, volumes and environment.

| App | Image | 1-click install | Quirk handled |
|---|---|---|---|
| Portainer | `docker.io/portainer/portainer-ce:lts` | 9443 (HTTPS), `/data`, the Docker socket | Notes: the setup token is in its log (`setup_token=`), and the admin must be created within 5 minutes. Port 8000 is left out (Edge agents only). |
| Uptime Kuma | `docker.io/louislam/uptime-kuma:2` | 3001, `/app/data` | Notes: no network file systems. |
| Vaultwarden | `docker.io/vaultwarden/server:latest` | 8222 → 80, `/data`, `SIGNUPS_ALLOWED` | Secure context: Open uses `localhost` (NOTE-29). No `DOMAIN` variable by default: the notes say to add it with a proxy. |
| Nextcloud AIO | `ghcr.io/nextcloud-releases/all-in-one:latest` | 8080 (HTTPS), the named volume, the socket read-only | AIO's fixed container and volume names, `init`, Docker's default bridge; single instance; companions (below). Notes: Nextcloud needs a domain and port 443. |
| Jellyfin | `docker.io/jellyfin/jellyfin:latest` | 8096, 7359/udp (discovery), `/config`, `/cache`, `/media` → the `media` share | Notes: DLNA needs host networking. |
| Pi-hole | `docker.io/pihole/pihole:latest` | 53/tcp+udp on the LAN address (fixed), 8053 → 80 (`/admin/`), `/etc/pihole`, a generated admin password | NOTE-18; `FTLCONF_dns_listeningMode: ALL` (its README, for bridge networks); no extra capabilities (only DHCP and NTP need them). |
| Syncthing | `docker.io/syncthing/syncthing:latest` | 8384, 22000/tcp+udp (fixed), config in the app's folder, `/var/syncthing/Sync` → the `backups` share | Runs as uid 1000 with the `nimbus-smb` group and umask 002, so SMB users can change what it syncs. |
| Home Assistant | `ghcr.io/home-assistant/home-assistant:stable` | host network, 8123, `/config`, `/run/dbus` read-only, TZ, 60 s to stop | Not privileged (below). |
| Grafana | `docker.io/grafana/grafana:latest` | 3000, `/var/lib/grafana` for its user 472, a generated admin password | Notes: Grafana sets the password on first start only ("Set once on first-run"). |
| Nginx Proxy Manager | `docker.io/jc21/nginx-proxy-manager:latest` | 80 and 443 (fixed), 81 (admin), `/data`, `/etc/letsencrypt`, TZ | Its first-run page creates the admin. |

- **Image tags** are each vendor's channel tag from its install guide (`lts`, `2`, `stable`, `latest`), so Update (pull and recreate) brings the vendor's current release. A template change still raises `template_version` (NOTE-60).
  - Nginx Proxy Manager's guide pins one release (2.17.1) while its README uses `latest`; the catalog follows the README.
  - Grafana's guide lists Enterprise as its default edition; the catalog uses the open-source image, `grafana/grafana`.
  - `catalog-images.sh` checks that every image and tag exists for linux/amd64.
- **Contract additions** (`manifest.py`, the schema and `SCHEMA.md`):
  - `webui.scheme` (`http` or `https`): Portainer and the AIO interface serve HTTPS with their own certificates. The Open link was always `http://` before.
  - `single_instance`: AIO's fixed names allow one install. A second plan or install is refused (409), and the Catalog card says "one per computer".
  - `companions` (`containers`, `volumes`, `network`). This implements NOTE-29, which nothing had done yet: AIO's own containers carry no Compose label, so Docker's listing never showed them.
    - `docker_api.containers(prefixes)` now also lists unlabelled containers by name prefix.
    - The Dashboard counts them with their app. The app's status ignores them, because AIO stops them itself for its backups.
    - Uninstall gives them 60 seconds to stop (`docker stop --time 60`; they hold databases), removes them and the `nextcloud-aio` network. Their volumes are removed only with "Delete data"; otherwise the log lists them. This follows AIO's own reset instructions.
  - Templates also see `timezone` (from `/etc/timezone` or `/etc/localtime`, else `Etc/UTC`) and `share_gid` (the `nimbus-smb` gid).
  - The engine keeps a template's `network_mode: "bridge"` (Docker's default bridge, as in AIO's compose.yaml). It drops `hostname` in host mode, because Docker refuses the two together.
  - Stricter parsing, so the schema and the parser agree: unknown keys, booleans written as strings, explicit `null`s where the schema has none, and host paths with `:` or `..` are all errors. `test_catalog.py` gives 82 manifests to both and expects the same verdict. It also checks the 6 things only the parser can (`id` equals the folder, unique ids, `webui.port` names a port, `3.0` as a version).
  - `check-catalog.py` also refuses anything in an app folder besides its three files.
- **Deviations and choices:**
  - The spec lists `icon` as a manifest field. The icon is the file `icon.svg` next to the manifest (NOTE-28), so the manifest has no `icon` key. The monograms use the UI's own colors with dark ink: Management blue, Monitoring green, Passwords amber, Files violet, Media coral, Network cyan, Smart home lime.
  - **Home Assistant isn't privileged.** Its guide sets `privileged: true` without saying what for, and that would give the container every device on the computer. The notes say to add a USB stick under `devices` in the Compose tab. TZ replaces mounting `/etc/localtime`.
  - **Syncthing runs on a bridge network.** Its README recommends host networking for local discovery, but published ports keep the 1-click port check working; the notes say to choose Host for local discovery.
    - The share is mounted at `/var/syncthing/Sync`, not as `/var/syncthing`, because the entrypoint chowns `$HOME` and would take the share's folder from `root:nimbus-smb`.
    - Syncthing v2 has no option to skip its default folder (checked in its source), so the share is where that folder lives.
  - **AIO publishes only 8080.** Ports 80 and 8443 serve the interface with a domain certificate and are optional, so AIO doesn't take port 80 from Nginx Proxy Manager. Pi-hole's page is on 8053 for the same reason.
  - **No `INITIAL_ADMIN_*` variables for Nginx Proxy Manager:** its `setup.js` logs that password in plain text.
  - Uptime Kuma gets no Docker socket: its README doesn't use one.
- **What couldn't run here.** No container can start in the dev container (NOTE-69), so installs are tested against the fake `docker` program, and `docker compose config` runs for real. Deliverable 11's QEMU test installs Jellyfin on the `media` share (the spec's step 3).

### Found while writing the workflows and docs (deliverable 10)

**NOTE-71 — Workflows, publishing, and the documentation.** Three workflows, Dependabot, the publishing tools, `README.md`, `docs/RELEASING.md`, `docs/SIGNING.md` and the first release notes.
- **The workflows.** Every job that needs Debian runs in a `debian:trixie` container with `tools/dev-shell.sh`'s base packages, so tests behave as they do locally. Containers run as root, so each job marks the checkout as a git `safe.directory`.
  - `ci.yml` has five jobs: lint (`tools/lint.sh` and the action pins), unit tests, the seven packages with lintian (throwaway key, NOTE-44), a matrix of 17 package and integration tests, and the network checks (`verify-trixie.sh`, Docker's key). Each matrix entry gets its own fresh container, because the tests install packages; the NAS, VM and Docker tests run privileged.
  - `release.yml` runs check, debs, publish, iso, release. `refresh.yml` runs resign (or init) and docker-key. Both share the concurrency group `nimbusos-publish` and never cancel a run. Each job asks for its own permissions: `contents: write` only to publish, `issues: write` only for the Docker key issue. Tags and outputs reach scripts through environment variables, never pasted into them.
  - The ISO job builds in `docker run --privileged` on the runner itself, after removing preinstalled toolchains, with the live-build tree on `/mnt` (live-build needs 40 GiB). It first waits until GitHub Pages serves the new `releases.json`: `build/build.sh` checks the repository (NOTE-04), and Pages deploys after the push.
  - `SHA256SUMS` is signed in the last job, so the long ISO build never sees the secret key.
- **The APT repository** (`tools/publish-apt.sh`; refines NOTE-52).
  - **Layout:** one pool per suite, `pool/<suite>/<package>/`, so pruning a suite needs no bookkeeping. Indexes are in `dists/<suite>/main/binary-amd64/`: every package is `Architecture: all`, and clients ask for amd64. Release files carry Origin and Label `NimbusOS`, Suite and Codename set to the suite's name, Architectures `amd64` and Components `main`. Every setting is an `apt-ftparchive -o` option, so the planned `apt/conf/` folder isn't needed.
  - **Found by testing apt-ftparchive 3.0.4:** it ignores a `Valid-Until` value given as an option. Only `APT::FTPArchive::Release::ValidTime` (seconds) writes the field, counted from the signing time. Re-signing writes `Date` and `Valid-Until` itself.
  - `APT::FTPArchive::DoByHash` writes the by-hash copies and `Acquire-By-Hash: yes`. Publishing keeps the index files the new and the previous Release name, and removes older ones.
  - A version already published with different contents is refused. Publishing identical files again changes nothing, so a failed release can be re-run.
  - The tag rules moved from `tools/deb-version.sh` into `build/lib/common.sh` (`nimbus_tag_to_debian`, `nimbus_tag_channel`), so publishing uses the same mapping without deb-version's working-tree checks.
  - Next to the repository: `releases.json` with a binary detached signature, `nimbusos-archive-keyring.asc` (for checking ISOs), and `.nojekyll`.
- **Signing in jobs:** `tools/import-signing-key.sh` loads the secret, then checks it holds one key, that the passphrase works (a test signature), and that the fingerprint is a committed key. It fails before anything is published.
- **The ISO assets:** `tools/split-iso.sh` writes the assets and the download instructions. The PowerShell join and check it prints was run on this Windows machine (Windows PowerShell 5.1): the parts joined back into the identical ISO.
- **Found by the Samba test:** a non-interactive Samba upgrade keeps the include line even without `UCF_FORCE_CONFFOLD`. Only an administrator choosing Samba's new file (`UCF_FORCE_CONFFNEW`) drops it, and nimbus-control's postinst block restores it exactly once. The test changes Samba's template, as a new release would, then runs Samba's own postinst through `dpkg-reconfigure`. Calling the postinst directly fails in `dpkg-maintscript-helper`, which needs dpkg's environment.
- **Lint:** `tools/lint.sh` runs shellcheck, ruff, `node --check`, the JSON and YAML parse, file modes and the Docker key check.
  - ruff 0.17.6 with the rule set in `ruff.toml` found 31 problems. 17 were fixed automatically (`datetime.UTC`, `TimeoutError`, `collections.abc`). By hand: `zip(..., strict=True)` where lengths must match, `raise ... from None`, the exact exception types in two tests instead of `Exception`, `def` instead of assigned lambdas, and lines over 140 characters.
  - shellcheck 0.10.0: SC2016 is off for the whole repository (`.shellcheckrc` gives the reason). Fixed:
    - an unquoted ExecStartPre command, now an array;
    - `A && pass || fail` chains, now if/else;
    - `local d="$(...)"`;
    - `rm -rf "$WORK/$1"`, now `${WORK:?}`.
  - Two sourced fragments got `shell` directives. shellcheck tracks variable names across sourced files, so the new tools avoid the array names `common.sh` uses.
- **Actions:** the latest releases at deliverable 10 were checkout v7.0.1, upload-artifact v7.0.2 and download-artifact v8.0.2. actionlint wasn't used: it isn't in Debian, and running its release binary was declined. Instead, the workflows were checked against themselves: every `needs`, every output and step id, matrix keys and permissions, and they parse as YAML (`tools/lint.sh`).
- **Licenses:** `LICENSE` is Debian's `/usr/share/common-licenses/GPL-3` (SHA-256 `3972dc97…`). `LICENSES/CC-BY-SA-4.0.txt` is SPDX's text at the commit `nimbusos-branding`'s `debian/copyright` pins (SHA-256 `cde7883b…`). The planned `LICENSES/GPL-3.0-or-later.txt` would only repeat `LICENSE`.
- **Documentation:**
  - `README.md` is for people visiting the project: download, verify, install, updates, security (NOTE-22, NOTE-45), building.
  - `docs/RELEASING.md` and `docs/SIGNING.md` are for the maintainer. The key's user ID has no e-mail address. It expires after 3 years, with the steps to extend it, rotate it, or act on a leak in an order installed systems can follow.
  - `docs/release-notes/v0.1.0.md` holds the first release's notes.
- **What couldn't be tested here:** the workflows on GitHub itself (runners, secrets, Pages). Every script they call is tested above. `release.yml` and `refresh.yml` need the one-time setup in `docs/RELEASING.md` (the key and the secrets); until then, refresh's weekly re-signing fails with the message that the secret is missing.
- **Found by the first `ci.yml` run on GitHub.** Two network checks failed. Both were reproduced locally and fixed:
  - **`tools/verify-trixie.sh` rejected `debhelper-compat (= 13)`** in every package's Build-Depends. It is a virtual package that debhelper provides with a version, and `apt-cache policy` shows no candidate for virtual packages. The check had never seen it: when it was written (deliverable 3), the packages didn't exist yet and were skipped. It now also accepts a dependency that a real package provides, with the provided version when the relation names one (`provided()`, from `apt-cache showpkg`'s Reverse Provides).
  - **`os-identity.test.sh` relied on `ntfs-3g`** having a pending security update. Debian 13.8 folded that fix into trixie, so no update was pending any more. The test now picks the package itself: the smallest one whose trixie-security version is newer than trixie's and whose trixie version installs on its own (apt simulates it first). It says so and skips those two checks if a point release has just folded every fix in.
  - **The VM test failed on GitHub's runners** after about 20 minutes. Force-off and delete failed with libvirt's "Failed to terminate process … with SIGKILL: Device or resource busy", and the cleanup minutes later hit the same message for the same process.
    - **The cause:** the processes libvirt killed became zombies that nothing reaped. GitHub starts a job container with `tail -f /dev/null` as PID 1, and libvirtd daemonizes QEMU, so a killed QEMU becomes PID 1's child and is never reaped. libvirt still finds it and reports it as busy. The privileged test containers lacked `--init` (the others had it); locally, bash is PID 1 and reaps.
    - **Reproduced here** with GitHub's setup: a privileged Trixie container with `tail -f /dev/null` as PID 1 hung until it was stopped, leaving five QEMU processes, swtpm and dnsmasq as zombies. The same test with `tini` as PID 1 (what `--init` provides) passed 7 of 7 in 8 seconds, with no zombies.
    - **The fix:** every test container in `ci.yml`'s matrix now runs with `--init`.
    - **A first guess was wrong:** I suspected nested KVM on the runners and ran the test with QEMU's emulator. It failed the same way, which led to the real cause, and that change was reverted.
  - To make failures readable without signing in, `ci.yml` now turns a failing test's error lines into annotations, which the run's summary page shows. That is how this cause was found.
  - All other jobs passed on GitHub's runners, including the privileged NAS and Docker tests.

### Found by the Trixie archive check (deliverable 3)

**NOTE-51 — `wsdd2` instead of `wsdd`.** Trixie has no `wsdd` package (§7). `wsdd2` (1.8.7, main) serves the same purpose: it makes the machine show up under Network in Windows Explorer through WS-Discovery. The storage list, the `nimbus-control` Depends, the services table, the alert rules and the Settings page all use `wsdd2` and its unit, `wsdd2.service`.

**NOTE-52 — The APT repository is built with `apt-ftparchive`, not `reprepro` (decided 2026-11-17).** Debian rolled `reprepro` back to 5.3.1 (`5.4.6+really5.3.2`). Tested in deliverable 5: a `conf/distributions` with `Limit: 3` fails with "Unknown header 'Limit'!". So a reprepro suite can hold only one version of each package, which breaks rollback with `apt install <pkg>=<version>`. reprepro would also need its database to survive between CI runs.

Deliverable 10 therefore uses `apt-ftparchive` from Debian's `apt-utils`, driven by `tools/publish-apt.sh`, which needs no database:
- **Pool:** `pool/` holds every published `.deb`. Pruning keeps the newest 3 versions of each package per suite, ordered with `dpkg --compare-versions` and never by file name or date, so `0.2.0~beta.1 < 0.2.0`.
- **Indexes:** `Packages`, `Packages.xz`, `Release`, `InRelease` and `Release.gpg`, with `Valid-Until` 14 days after signing. Acquire-By-Hash is enabled (`APT::FTPArchive::DoByHash`), so clients that start an update while a publish is replacing the indexes still get consistent files.
- **Re-signing:** `refresh.yml` only re-signs. It rewrites the `Date` and `Valid-Until` fields of each existing `Release` (the index hashes don't change) and signs it again. It never regenerates, adds or prunes packages.
- **Test:** `tools/tests/publish-apt.test.sh` builds fake `.deb` files and proves pruning keeps exactly the right 3 versions, including the `~beta` ordering. It also checks that a re-sign leaves the package indexes byte-identical.

`aptly` (also in Trixie) was considered and rejected: it has no `Valid-Until` option, which the weekly re-signing relies on.

**NOTE-53 — Package-list format rules.** live-build passes each list through `printf` as a format string and reads it with `read` (`Expand_packagelist` in live-build 1:20250505). So:
- a `%` or `\` anywhere in a list, comments included, corrupts it;
- a line starting with `!` is executed as a shell command;
- `#if`, `#nif` and `#endif` are conditionals;
- there are no trailing comments: `pkg # note` would install packages named `#` and `note`.

NimbusOS lists therefore hold one package name per line and plain `# ` comment lines only, and `tools/verify-trixie.sh` and `tools/gen-desktop-depends.sh` reject anything else.

---

## 7. Verification status

`tools/verify-trixie.sh` checks every package and flag against the real archive. CI runs it on every push (deliverable 10), and anyone can run it through `tools/dev-shell.sh`. It lints the lists against live-build's format rules, gives every listed package a candidate, resolves all chroot lists together with Recommends, resolves each binary list on its own, checks the dependency fields of `packages/*/debian/control`, and runs `lb config` with `build.sh`'s exact flag list, plus a bogus flag as a control. ruff is CI-only and not checked (NOTE-43).

**Run of 2026-11-17** (deliverable 3): `debian:trixie` container (Debian 13.7) via `tools/dev-shell.sh` on Podman, mirror deb.debian.org, live-build `1:20250505+deb13u1`, with `--skip-docker` because Docker's key arrives in deliverable 4. Result: **PASSED**. 8 chroot lists and 3 binary lists, 109 distinct packages, all present. The chroot lists resolve together to 1,906 packages; the ISO pool lists resolve to 41 (BIOS), 55 (UEFI) and 54 (LUKS) packages. `lb config` accepts all 78 arguments and rejects a bogus one.

| Item | Result | Consequence |
|---|---|---|
| `snapraid` | 12.4-1, main | Present; no fallback package needed (NOTE-26) |
| `wsdd` | **not in Trixie** | Replaced by `wsdd2` 1.8.7+dfsg-1.2, main (NOTE-51) |
| `samba-vfs-modules` | 2:4.22.11+dfsg-0+deb13u1, main | Present |
| `fonts-inter` | 4.1+ds-1, main | Present (`fonts-inter-variable` exists too, unused) |
| `--chroot-squashfs-compression-type` | accepted; stored `LB_CHROOT_SQUASHFS_COMPRESSION_TYPE="xz"` | xz squashfs as specified |
| `--uefi-secure-boot` | accepted; stored `LB_UEFI_SECURE_BOOT="auto"` | Debian's signed shim/GRUB used when available |
| `plasma-discover-backend-packagekit` | does not exist | The backend is built into `plasma-discover` (`packagekit-backend.so`), as planned |
| `reprepro` | `5.4.6+really5.3.2` (reports 5.3.1): no `Limit:` | Assumption A5 was wrong; see NOTE-52 |
| Docker CE packages | **checked in deliverable 4**: all five present in `https://download.docker.com/linux/debian trixie stable`, signed by the pinned key | See the second run below |
| `packages/*/debian/control` | not written yet | Checked automatically once deliverable 5 exists |

**Second run of 2026-11-17** (deliverable 4), same setup but **with Docker's repository**. Result: **PASSED**.
- **Docker key:** fetched from `https://download.docker.com/linux/debian/gpg` by `tools/fetch-docker-key.sh`. It holds exactly one primary key, `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`, "Docker Release (CE deb)", with no expiry. The committed file's SHA-256 is `1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570`. `--verify-local` and `--check` both pass.
- **Packages:** Docker's `trixie` `InRelease` verifies against that key. 114 distinct packages are all present, including `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin` and `docker-buildx-plugin`. The chroot lists, Docker included, resolve together to 1,914 packages.
- **Tests:** `tools/tests/docker-archive.test.sh` 9/9 and `tools/tests/image-apt-check.test.sh` 7/7, the latter using the real rendered source files.

**Deliverable 5, part 1** (2026-11-17, `debian:trixie` container via `tools/dev-shell.sh`):
- **Build:** `nimbusos-archive-keyring`, `nimbusos-docker-keyring`, `nimbusos-release` and `nimbusos-desktop` build with `tools/build-debs.sh --lintian` (lintian 2.122.0, `--fail-on error,warning`; overrides in NOTE-54). `nimbusos-desktop` gets 109 dependencies: 103 from the lists plus 6 NimbusOS packages.
- **`pytest packages/nimbusos-release/tests`:** 14 passed.
- **`apt-integration.test.sh`:** 6 passed. Real apt 3 output for an unsigned repository is classified as `signature`, and for an unresolvable host as `dns`.
- **`install.test.sh`:** 22 passed. It covers the os-release diversion and its removal, `update.conf` creation and preservation, the beta source, `#clear` leaving exactly 2 Debian-Security origins, the enabled timers, and purge.

**The `apt-cache policy` proof** (spec 4.2), produced by `apt-integration.test.sh`. Loopback hosts stand in for Debian (127.0.0.2), NimbusOS (127.0.0.1) and Docker (127.0.0.3); the NimbusOS and Docker repositories also offer a newer `bash`, as if compromised; the pin files are the shipped ones, rendered for those hosts:

```
bash:
  Installed: (none)
  Candidate: 5.2.37-2
  Version table:
     99.0 1
          1 http://127.0.0.1/nimbusos ./ Packages
     98.0 1
          1 http://127.0.0.3/docker ./ Packages
     5.2.37-2 500
        500 http://127.0.0.2/debian ./ Packages
nimbus-control:
  Installed: (none)
  Candidate: 0.2.0
  Version table:
     0.2.0 500
          1 http://127.0.0.1/nimbusos ./ Packages
docker-ce:
  Installed: (none)
  Candidate: 29.0
  Version table:
     29.0 500
          1 http://127.0.0.3/docker ./ Packages
```

How to read it: the NimbusOS and Docker repositories offer `bash` 99.0 and 98.0, yet both are pinned to 1, so Debian's 5.2.37-2 (priority 500) stays the candidate. NimbusOS's own `nimbus-control` and Docker's `docker-ce` get 500. In the version table, the left number is the priority of the version; the indented line shows the source's default (1).

**Review fixes after part 1** (2026-11-17; every test ran in a fresh `debian:trixie` container in `dev-shell.sh`'s new clone mode). All pass:
- **Packages:** 76 Depends and 37 Recommends in the built `nimbusos-desktop`. Only `nimbus-update` and `nimbus-flathub-setup` are executable inside the packages. lintian is clean (NOTE-54).
- **Test results:**

| Test | Result |
|---|---|
| `check-file-modes.sh` | 83 tracked files OK; flipping two bits made it fail, with the fix commands |
| `pytest` (`nimbus-update`) | 17 passed; `install` and `auto` pass `APT::Install-Recommends=true` and never `--no-install-recommends` |
| `apt-integration.test.sh` | 6 passed |
| `install.test.sh` | 22 passed |
| `recommends.test.sh` | 12 passed |
| `os-identity.test.sh` | 11 passed (network) |

- **`recommends.test.sh`** walks through four steps:
  1. Install the stand-in v1; removing `firefox-esr` keeps `nimbusos-desktop` installed.
  2. Publish v2 and run `nimbus-update auto`: it installs the newly recommended `new-app` and does not reinstall `firefox-esr`.
  3. Publish v3 and run `nimbus-update install`: same result.
  4. Remove `nimbusos-desktop`: `status` shows the warning.
- **Two test artifacts found while writing it:**
  - Debian's `libc6` declares `Breaks: firefox-esr (<< 91~)`, so a stand-in `firefox-esr 1.0` is (correctly) never installed; the stand-in now uses version 140.5.0esr-1.
  - Python's test web server answers 304 if a republished `Release` has the same one-second timestamp; the test waits a second before each publish.

**Deliverable 5, part 2** (`nimbusos-branding`, 2026-11-17, `dev-shell.sh` clone mode):
- **Build:** the package builds, and lintian is clean with no overrides. Its one finding, `command-with-path-in-maintainer-script` for `[ -x /usr/sbin/update-grub ]`, was fixed by using `command -v`.
- **`packages/nimbusos-branding/tests/install.test.sh`:** 57 passed. It covers every rendered file present, no unfilled token, all three GRUB font names matching `theme.txt`, valid JSON, the GRUB drop-in adding `splash` exactly once even if sourced twice, the XDG env script, and Plymouth: `text` → `nimbusos` on install, kept on reinstall, back to `text` on removal, with no `Theme=` left behind.
- **Alternatives rework (NOTE-56), same test:** 92 passed, lintian clean, with `desktop-base` 13.0.4 installed first. Checked:
  - `desktop-theme` is NimbusOS at 80, auto mode, strictly the highest;
  - the background, background XML, lockscreen XML, login background, `DebianTheme`, `desktop-grub.png`, `grub_background.sh` and `debian-theme` Plymouth links all resolve to NimbusOS files, and every image the XML files name exists;
  - no desktop-base alternative has a broken link or target;
  - `--set desktop-theme emerald-theme` survives a reinstall in manual mode, the login background follows it, and `--auto` returns to NimbusOS;
  - a desktop-base reinstall keeps NimbusOS active and re-registers its active-theme GRUB entry;
  - removal returns to `ceratopsian-theme` in auto mode, with no NimbusOS entry and no broken link;
  - Plymouth `ceratopsian` → `nimbusos` → `ceratopsian`.
- **Rendering:** checked by eye from the built package. The wallpaper shows a dark sky with a blue glow top left, a violet glow bottom right, soft cloud bands and faint stars. The logo is one cloud with a blue-to-violet gradient: a first render showed a seam because each shape used its own gradient box, fixed with one `userSpaceOnUse` gradient.
- **Two build errors found and fixed:** XML forbids `--` inside comments, and an SVG comment mentioned the CSS variable `--bg`.

**Deliverable 5, part 3** (`nimbus-control`, `nimbus-catalog`, 2026-11-18, `dev-shell.sh` clone mode, all suites in one fresh container):
- **Build:** `nimbus-control` builds and lintian is clean, with two overrides whose reasons are in the file (the one `systemctl disable`, and `WantedBy=docker.service`). The `nimbus-catalog` test build is lintian-clean with no overrides.
- **`control-center/tests/test_migrate.py`:** 10 passed (pytest 8.3.5). Covered:
  - fresh install;
  - an upgrade from a fake older version through steps 2 and 3, in order, with a backup;
  - a failing step and a broken step file, both restored byte for byte with one message;
  - downgrade refused, state without `schema_version` refused, a second run that changes nothing, at most 5 backups, and a gap in step numbers rejected.
- **`packages/nimbus-control/tests/maintscripts.test.sh`:** 82 passed. The container behaves like the image chroot: no systemd, and policy-rc.d exits 101. Covered:
  - every Depends and Recommends resolves in Trixie plus Docker's repository;
  - the static check, with a self-test;
  - first install of a fake older version: accounts, groups, modes, the include once, `testparm` accepting the result (guest account, mDNS off, NetBIOS off, SMB2_10 minimum), adduser keys, nmbd off, the default network on autostart, units enabled, starts only via `deb-systemd-invoke`, and ExecStartPre creating schema 1 as `nimbus-control`;
  - upgrade: the administrator's nmbd, wsdd2 and EXTRA_GROUPS choices kept; a restart because files changed, but only a start on a reinstall with the same files; user data and state unchanged;
  - the installed nimbus-migrate as the service user: step 2 with a backup it owns, a failing step 3 restored with one message, and a downgrade refused;
  - remove keeps everything;
  - purge: user data byte for byte with owners, the SMB user and password, the fstab block, `smb.conf` byte-identical, adduser restored except the administrator's line;
  - no docker, virsh, smbcontrol, invoke-rc.d or service call at any point.
  The purge message:

  ```
  nimbus-control: removed the Control Center's settings, caches and Samba configuration.
  Samba no longer serves the NimbusOS shares. Your data was kept; delete it yourself if
  you no longer need it:
    /var/lib/nimbus-control/stacks     app stacks with their data and volumes
    /var/lib/nimbus-vms                VM disks (images/) and ISO files (isos/)
    /mnt/user                          the storage pool
    /mnt/disks                         data disks
    /mnt/parity                        parity disks
    /srv/nimbus/shares                 shared folders on the system disk
    /etc/nimbus/snapraid.conf          SnapRAID configuration (needed to use the parity)
    /var/lib/nimbus-privd              SMB user list and /etc/fstab backups
    /etc/fstab                         the '# BEGIN nimbus' block that mounts your disks
    SMB users                          alice (with their Samba passwords)
    system accounts                    nimbus-control, nimbus-guest and the group nimbus-smb
  ```

- **`packages/nimbus-catalog/tests/catalog.test.sh`:** 30 passed. Covered:
  - 13 check-catalog cases (a good catalog, and 12 rejections each with its own message);
  - the build failing on a manifest without `template_version`;
  - no maintainer scripts in the package;
  - an upgrade with a changed grafana template that leaves the installed stack byte for byte unchanged, with "Template update available" true for grafana only;
  - a purge that leaves the stack alone.
- **Found while testing:**
  - compat 13 doesn't run `dh_installsysusers`;
  - `--no-start` also drops debhelper's stop on removal, so the package has its own prerm (NOTE-61);
  - `testparm` prints SMB2_10 under its canonical name `SMB2`, so the test compares with an explicit SMB2_10 instead;
  - running the catalog test first left a simulated state directory behind, which nimbus-migrate rightly refused (exit 3). Both tests now require a clean `/var/lib/nimbus-control`, and the catalog test removes its own.

**Deliverable 6** (live-build hooks, 2026-11-18, `dev-shell.sh` clone mode):
- **`tools/tests/hooks.test.sh`:** 72 passed. It installs five real NimbusOS packages (the other two as stand-ins at the same version) into a container standing in for the chroot. Covered:
  - 0100 adds Flathub, leaves `/tmp`, `/var/tmp`, `/root` and the machine-id unchanged, is a no-op the second time, and fails clearly without its setup script;
  - 0200 works on Trixie's real Calamares files: valid keys, idempotent, an existing `NoDisplay=false` replaced, a renamed file fails the build;
  - 0300 enables a unit from the includes, fails on one that didn't reach the chroot, and accepts none;
  - 0900 in that partial image: the 13 checks that should pass do. Exactly two fail, each naming only what is really missing there: list packages such as plasma-desktop, sddm and calamares; and five services (display manager, NetworkManager, docker, containerd, the live-only tmpfs). The 10 services that are enabled are not named. Twelve deliberate breakages each produce a FAIL naming the problem, and restoring them returns to the same two failures.
- **`tools/tests/image-initrd-check.test.sh`:** 9 passed, against real initramfs images from Trixie's kernel 6.12.117+deb13-amd64. Covered: the check passes with the nimbusos theme, fails naming the missing files with theme `text`, passes again after switching back, and fails on a non-initramfs and a missing file.
- **shellcheck:** the hooks, both tests, `build.sh` and `common.sh` have no warnings. Three false positives in `build.sh` and `common.sh` (variables read through `compgen` or by callers) now carry a targeted disable with the reason.
- **Earlier tests still pass:** `deb-version` 40, `image-apt-check` 7, `docker-archive` 9.
- **Found while testing:** the first verify-hook draft had three bugs, all fixed before the runs above. A correct Plymouth theme would have been reported as a failure. `grep -q` at the end of a pipe under `pipefail` could turn a match into a failure. And `readlink -f` on a missing link gave a confusing message. The tests now cover each case.

**Deliverable 7** (live files and boot menus, 2026-11-18, `dev-shell.sh` clone mode):
- **`tools/tests/live-files.test.sh`:** 50 passed. Covered:
  - all Calamares files are valid YAML;
  - every module `settings.conf` names exists in Trixie's `calamares` or `calamares-settings-debian`, and each instance has its config;
  - both launchers pass desktop-file-validate;
  - the mounts are conditional on `boot=live`, and `Where=` matches the unit name;
  - `sources-final`: `apt-get update` works without a warning, all 12 pairs are fetched, a second run changes nothing, and it fails clearly without Debian's keyring;
  - `live-session-setup`: does nothing outside a live session, then puts the launcher on the desktop once;
  - `target-cleanup` after hooks 0300 and 0400: all 16 live files gone, both mounts disabled with no link left, the listed `/etc/issue.net` kept because it belongs to base-files, and a missing list fails.
- **`tools/tests/bootloaders.test.sh`:** 31 passed, using live-build's own scripts. Covered:
  - GRUB: three entries with kernel, initrd and the right options (safe graphics adds `nomodeset`; fail-safe keeps the live user and groups); no "Live system"; no leftover token; every cfg passes `grub-script-check`; the theme fonts exist;
  - ISOLINUX: the same three entries, the first as default, the NimbusOS title, the timeout;
  - the splash: 640×480 and 800×600 PNGs, looked at.
- **`tools/tests/hooks.test.sh`:** 90 passed (was 72). Covered:
  - hook 0300 with the real includes enables both mounts;
  - hook 0400 lists every file and itself, and refuses a file owned by `base-files` and one that didn't reach the chroot;
  - the verify hook reports `installer`, `live-files` and `installer-launcher` as ok with the real files;
  - nine more deliberate breakages each produce a FAIL naming the problem: no cleanup step, Debian branding, Debian's `sources-final` back, `nimbus-control` missing from users.conf, users.conf not YAML, a wrong EFI id, Calamares not removed, no live-file list, Debian's "Install Debian" visible.
- **Still pass:** `deb-version` 40, `image-apt-check` 7, `docker-archive` 9, `image-initrd-check` 9. shellcheck has no warnings for the new sh and bash scripts.
- **Found while testing:**
  - live-build's `binary_syslinux` expects the kernel in `binary/live/` already, so the test tree now has it;
  - my own comments quoting "Live system" tripped a check, which now skips comments;
  - `apt-cache policy` and `apt-get indextargets` leave out suite/component pairs whose index is empty, so the test checks what apt fetches (`--print-uris`).

**Deliverable 8, batch 1** (foundation and Updates page, 2026-11-18, `dev-shell.sh` clone mode):
- **pytest, 83 passed.** It covers:
  - validators: names, ports, and `path_under` with `..`, a symlink escape and a look-alike prefix;
  - the privd server on a real socket: another uid refused; unknown verb, broken line, undeclared, missing and invalid arguments rejected with the field named; secrets redacted in the audit; streaming; busy locks; clean failures;
  - the update verbs: exact argument lists with no shell, `systemd-run` for install, journal follow, exit-1 documents kept;
  - the backend: the 0640 token file reused; Host and Origin checks; `/api` needing the token; security headers; launch codes (once, 30 s); WebSocket refusals for the right reason; the Updates API; privd errors mapped to statuses; the sidebar count.
- **The 10 earlier `test_migrate` cases still pass.** Every frontend module parses (`node --check`).
- **`control-center/tests/integration/installed-smoke.sh`, 14 passed.** It installs `nimbus-control`, `nimbusos-release`, the keyring and branding, starts privd through `systemd-socket-activate` (socket 0660 `root:nimbus-control`) and the backend as `nimbus-control`. Checked:
  - the token file mode; the page and CSP; fonts from Debian's packages; the logo;
  - 401 without the token, 403 for a foreign Host, the launch code working once;
  - the installed os-release;
  - the real `nimbus-update` status through privd, and "Check now" reporting `('nimbusos', 'unreachable')` (the repository isn't published yet) as an error;
  - a desktop user in `nimbus-control` refused by privd;
  - the audit trail.
- **Package:** `nimbus-control` builds and is lintian-clean (same two overrides). The maintainer-script test still passes 82 of 82 with the new contents.
- **Screenshots** (`ui_preview.py`, headless Chromium with Debian's fonts) were looked at: the Updates page at 1400 px and full height, the 800 px layout (tab strip, stacked panels), and the signed-out page. Three things were fixed after looking: extra space above the facts list, radio-card text drifting apart in equal-height cards, and a meaningless "12:00 AM" on date-only release dates.

**Deliverable 8, batch 2** (Apps, 2026-11-18, `dev-shell.sh` clone mode, NOTE-66):
- **pytest, 222 passed** (139 new). The new tests use a fixture catalog of three demo apps (`control-center/tests/fixtures/catalog`), a fake Docker SDK and a fake `docker` program that logs its arguments.
  - `test_manifest` (32): defaults filled in; 26 kinds of broken manifest refused with the app and field named; the catalog skips links and lists broken apps, picks up changed and removed apps without a restart, and applies a JSON schema.
  - `test_compose` (35): YAML injection through values impossible; the sandbox blocks Python internals; what the engine sets on the main service; project-scoped and pinned volume names; 17 allowed and refused bind mounts (`/etc`, `~`, `..`, a link out of the stack folder, a look-alike share path); `docker compose config` errors reported, and no folder left behind.
  - `test_apps` (62): the whole API through the real app. Covered: public icons and nothing else public; 1-click plans (busy ports, LAN binding, fixed port 53, `share_hint`, secrets, unique names); 22 invalid inputs refused with their field before anything runs; install, every action, edit (settings and a hand-edited file), the template update with its masked diff, and uninstall with and without data, each with the exact `docker compose` argument lists; a failed pull keeps the app listed; a rejected file leaves nothing; status for 12 container situations; live logs; one job per app; plain job errors.
  - `test_privd` (+10): the stack verbs on a real socket, as root, so the chown to uid 472 is real; bad arguments; links refused in all three places.
- **Against the real Docker Compose** (`docker-ce-cli` and `docker-compose-plugin` from Docker's repository, verified with the pinned key; `docker compose version` v5.8.0): four tests that are skipped where Compose isn't installed passed. Compose accepts the engine's output for all three fixture apps: LAN-bound UDP 53, an external network, a named volume, host networking with the read-only Docker socket. It reads every value back exactly as typed, including `$`, spaces, `:`, `#`, quotes and a newline. Found here: Compose's `config` output escapes `$` as `$$` again.
- **`installed-smoke.sh`, 20 passed** (6 new). The new checks:
  - the Apps page is served;
  - `/api/apps` answers "Docker isn't running" without failing;
  - the catalog is empty without `nimbus-catalog`;
  - as the service user, through privd's socket, `stack.prepare_volume` hands `/var/lib/nimbus-control/stacks/smoke/config` to 472:472;
  - the service user itself can't delete a file a container wrote there;
  - `stack.delete_data` removes the whole folder.
- **Package:** `nimbus-control` builds and is lintian-clean. The maintainer-script test passes 82 of 82. Every frontend module parses (`node --check`). `check-file-modes`: 232 files OK.
- **Screenshots looked at:** the Installed list (running, starting, stopped, error with its reason, template update) at 1400 and 800 px, the Catalog, Advanced install, its Compose tab, and the template diff. Fixed after looking:
  - the Settings panel stayed visible under the Compose tab: `display: grid` beat the `hidden` attribute, so `[hidden]` now always wins;
  - app names wrapped mid-word;
  - category tags stretched across the card;
  - the folder path was cut off;
  - the Restart field sat lower than Network;
  - action buttons ran off the table at 800 px;
  - secret values showed in the template diff;
  - an app without an icon showed an empty square, and now shows its initial;
  - animations were caught half-way in the screenshots, which now use reduced motion.
- **Found by the tests:**
  - the state file stores settings sorted, so a rebuilt file listed variables in another order and the template diff showed changes that weren't there;
  - a bad name in a plan request gave 500 instead of 422; `ValidationError` now maps to 422 everywhere;
  - a broken catalog JSON file's error didn't name the app.

**Deliverable 8, batch 3** (Virtual Machines, 2026-11-18, `dev-shell.sh`, NOTE-67):
- **pytest, 310 passed** (92 new); 11 are skipped here: 4 need Docker's CLI and 7 need a running libvirtd. The new tests:
  - `test_vm_xml` (33): both presets with every option, VNC local with its password, escaping, reading VMs made by other tools. 24 of them run libvirt's `virt-xml-validate` on every valid combination.
  - `test_isos` (22): a chunked upload byte for byte and 0644; 11 bad names; sizes and free space; order; never replacing a file; links in three places; cancel and cleanups.
  - `test_vms` (37): the API through the real app with `FakeLibvirt`, which keeps each domain as its real XML. It covers the list and usage maths, libvirt down, no KVM, 18 refused settings with their field, a refused domain leaving no disk, actions, CD and autostart, delete with and without disks (never outside the pool), console token files, ISO upload over HTTP, used-by, the token requirement, noVNC, the CSP and the sidebar count.
- **Against a real libvirtd** (`tests/integration/vm-libvirt.sh` in a privileged throwaway container: libvirt 11.3.0, QEMU 10.0.13, TCG because the container can't use `/dev/kvm`): `test_vm_libvirt`, 7 passed. Checked:
  - a Linux UEFI VM, a Windows VM (`OVMF_CODE_4M.ms.fd`, `secure='yes'`, swtpm state) and a BIOS VM all start and run;
  - CPU time moves; the CD is ejected and inserted live; reboot, shutdown, force off and autostart work;
  - a console token reaches the VM's VNC server through websockify, which offers VNC password authentication; a wrong token is refused;
  - deleting with the disk removes the disk, the UEFI variables and the TPM state.
  - Container-only settings in `qemu.conf` (QEMU as root, no cgroups, AppArmor or xattrs) are explained in the script.
- **The live console, looked at:** `ui_preview.py --live-vm` started a real VM and the real backend and websockify, then took screenshots over the DevTools protocol after real seconds. They show the VM list with live CPU (40 %) and RAM (137 MiB of 512 MiB), and the console modal showing the VM's UEFI shell, connected with the VNC password through noVNC 1.6.0 from `/novnc/`.
- **`installed-smoke.sh`, 32 passed** (12 new). The new checks:
  - the VM page, console page, `lib/vnc.js` and noVNC are served;
  - the VM list answers "libvirt isn't running" without failing;
  - a chunked ISO upload lands in `/var/lib/nimbus-vms/isos` as `nimbus-control:nimbus-smb` 0644, byte for byte, with no part file left, is listed, and can be deleted;
  - websockify, started exactly as `nimbus-websockify.service` starts it (as `nimbus-control`), reads a token file from `/run/nimbus-control/vnc-tokens`, proxies to a VNC stand-in, and refuses an unknown token.
- **Package:** `nimbus-control` builds and is lintian-clean. The maintainer-script test passes 82 of 82. Every frontend module parses.
- **Screenshots looked at** (fake data): the Machines list (running with usage, paused on a disk error, stopped Windows that starts with the computer, a virt-manager VM) at 1400 and 800 px, the ISO library, Create VM for Linux and for Windows, Options and Delete. Fixed after looking:
  - VM names were cut to three letters;
  - the table ran off the window at 800 px, so the Resources column now hides under 1100 px;
  - "64 GiB" broke over two lines;
  - a driver option was too long for its field.
  - Choosing Windows now also suggests a Windows ISO and the virtio-win ISO from the library.

**Deliverable 8, batch 4** (Storage, Shares and Settings, 2026-11-18, `dev-shell.sh`, NOTE-68):
- **pytest, 407 passed** (97 new); 11 are skipped as before (Docker's CLI, libvirtd).
  - `test_storage_privd` (31): disk roles behind LUKS and LVM, the live medium, SMART summaries and cache, the fstab block (backups, the rest of the file kept), seven format refusals and the exact commands, mount, unmount and the pool (mergerfs line, live branch change, parity size, SnapRAID configuration, single mode with a busy pool), `snapraid diff` counts.
  - `test_samba_privd` (31): share checks, the rendering of every option, `share.apply`'s candidate check and rollback, the include put back, a missing disk, links, the isos bind, `share.delete_data`, SMB users (passwords never in argv), Test connection's 0600 file, and the systemctl calls of `samba.settings`.
  - `test_nas_api` (31): the API with `FakeNas`, covering the overview, disk jobs, pool changes, 12 wizard refusals and its exact privd calls, shares saved only when applying worked, field errors, users and settings.
  - `test_nas_scripts` (4): the recycle-bin and SnapRAID timers.
- **`nas-integration.sh`, 35 passed** (privileged throwaway container, from source). Three tmpfs mounts stand in for formatted disks. The checks cover:
  - the mergerfs pool mounted from its fstab line and changed in place;
  - Pooled + parity with a real SnapRAID sync and scrub, and the diff counts;
  - SMB users made by adduser and smbpasswd;
  - `share.apply` with the packaged `nimbus.conf`;
  - smbclient as two users and a guest: Secure, Private (read-only for one user) and Public behave as designed;
  - files arriving as `nimbus-smb` 0664 on a pool disk;
  - the recycle bin and its cleaner;
  - Avahi with `_adisk`, and the isos share bound onto the ISO library with its ISO moved in;
  - `share.delete_data`, user removal, and `nimbus-netfix` against real iptables.
  - It found mergerfs's 4 GiB `minfreespace`, and Debian's unconfigured share sections (NOTE-68).
- **`installed-smoke.sh`, 42 passed** (10 new). The new checks:
  - the storage overview through privd;
  - an SMB user and a Private share made through the API with the installed package, privd as root and the packaged drop-in reached through postinst's include;
  - the folder's owner and mode, testparm, and the Avahi file;
  - smbd serving the share to that user (files `nimbus-smb` 0664) and not to guests;
  - Test connection, and the default settings.
- **Package:** `nimbus-control` builds and is lintian-clean with the three new scripts and the new Depends. The maintainer-script test passes 82 of 82.
- **Screenshots looked at:** shares with their tags, usage and connection strings; disks with roles, health (bad sectors, asleep) and temperature; the pool with parity status; SMB users; the share editor (also at 800 px); formatting a disk; the NAS wizard's disk, mode and confirm steps; Settings. Fixed after looking:
  - the parity explanation stuck to the list above it;
  - the wizard's confirm step didn't say which disk becomes what.

**Deliverable 8, batch 5** (Dashboard and metrics, 2026-11-23, `dev-shell.sh`, NOTE-69):
- **pytest, 432 passed** (25 new); 11 are skipped as before (Docker's CLI, libvirtd).
  - `test_metrics` (15): the stream client (chunked documents split across reads, plain bodies, Docker's error message, a missing socket), the CPU and memory formulas (cgroup v1 and v2), `HostSampler`'s own CPU deltas, the split never below 0, a sample with containers (`FakeDockerd`) and VMs, history, samples never overlapping, a restart loop, an event waking the collector in under 1.5 s, sampling stopping without viewers, the storage copy, both routes, and a snapshot that is valid JSON.
  - `test_alerts` (7): every rule and its action, the order by severity, and libvirt's state and reason mapping.
  - `test_apps` (1): diagnose names the failing setting. `test_storage_privd` (2): `snapraid diff`'s escaped paths, and the unprotected bytes from a real diff's line forms.
- **`docker-streams.sh`, 6 passed** (privileged throwaway container, Docker 29.10 from Docker's repository): `/events` delivers create and destroy through the collector's filter; `stats?stream=1` sends document after document with the fields the formulas read; the formulas accept a real document; an unknown container gives Docker's 404 message.
- **`vm-libvirt.sh`, 7 passed** (libvirt 11.3.0, QEMU 10.0.13, TCG): the statistics call for a running VM (state, vCPUs, memory, resident size, CPU time moving), a VM paused with `virsh suspend` read back as "paused by the user", and a stopped VM in the same call.
- **`nas-integration.sh`, 35 passed**: `snapraid.status` now also checks the unprotected size (two files, one named `my film (1).mkv`: 8 + 3 bytes). This found the parser bug in NOTE-69.
- **`installed-smoke.sh`, 46 passed** (4 new): `views/dashboard.js` and `lib/charts.js` are served; `/api/dashboard` measures the host with the real psutil and reports Docker and libvirt down; `/ws/metrics` pushes a new sample about 2 seconds after the previous one.
- **Package:** `nimbus-control` builds and is lintian-clean. The maintainer-script test passes 82 of 82.
- **Screenshots looked at:** the Dashboard at 1400 px, full height and 800 px (cards with sparklines, the split with its legend, alerts with severity stripes and actions, apps and VMs with live usage, Quick open), and Fix opening dns's settings at the port another program holds. Fixed after looking:
  - System & desktop showed 0 % (the preview's host load was below the apps' and VMs' sum);
  - the preview's dashboard had no app usage under Chromium's virtual time, so the dashboard is now shot after real seconds;
  - Quick open's tile letters were lowercase;
  - Fix had nothing to highlight in the preview, so the preview now holds the failing app's port.

**Deliverable 9** (the app catalog, 2026-11-23, `dev-shell.sh`, NOTE-70):
- **pytest, 531 passed** (99 new). 21 are skipped: the 11 as before, and 10 that need Docker's CLI, which `catalog-images.sh` runs.
  - `test_catalog` (97): the ten apps load with no errors and pass the schema and the parser; the schema and the parser agree on 82 manifests, and the parser catches the 6 things a schema can't; templates quote every value and use the documented images; icons are plain SVG; the default host ports don't collide.
  - Through the API with the fake `docker` program, `test_catalog` also covers:
    - every app's 1-click plan;
    - the spec's quirks (AIO, Home Assistant, Pi-hole, Jellyfin, Syncthing);
    - all ten installed side by side;
    - AIO refused a second time, its companions, and uninstall with and without "Delete data".
  - `test_compose` (1): Docker's default bridge, `hostname` in host mode, `timezone` and `share_gid`. `test_metrics` (1): companions count with their app.
- **`catalog-images.sh`, 11 passed** (throwaway container, skopeo 1.18.0, Docker CLI 29.10.0, Compose v5.8.0):
  - All ten images exist for linux/amd64. Version labels: Home Assistant 2026.11.2, Jellyfin 12.4, Pi-hole 2026.11.0, Syncthing v2.1.7, Vaultwarden 1.37.5. The other five carry none; they were built 2026-11-10 to 2026-11-19.
  - The real `docker compose config` accepts every app's 1-click file and the fixture apps (14 checks).
- **`installed-smoke.sh`, 49 passed** (3 new): installing `nimbus-catalog` next to the running service lists its ten apps with no restart; an icon is served; a 1-click plan on the installed system has its hostname and the real `nimbus-smb` gid.
- **`catalog.test.sh`, 32 passed** (2 new): the real catalog passes `check-catalog.py`, and a stray file inside an app is refused.
- **Packages:** `nimbus-catalog` builds from `catalog/` ("10 app(s) OK") and is lintian-clean, as is `nimbus-control`. The maintainer-script test passes 82 of 82.
- **Screenshots looked at:** the Catalog tab with the ten apps (monograms on their category colors), and Advanced install for Pi-hole: its DNS ports on the LAN address, the fixed-port warning when port 53 is taken, and its notes.

**Deliverable 10** (workflows and docs, 2026-11-23, `dev-shell.sh`, NOTE-71). `ci.yml`'s lint and unit jobs, run as the workflow runs them, in one fresh container:
- **`tools/lint.sh`:** shellcheck 0.10.0 on 57 scripts, ruff 0.17.6, `node --check` on 13 files, 16 JSON and 4 YAML files, file modes, Docker's key: all clean.
- **pytest, 548 passed** (the Control Center and nimbus-update's tests together); 21 skipped as before.
- **`publish-apt.test.sh`, 38 passed** (new):
  - init;
  - pruning out of order and the `~beta` order (`0.2.0~beta.1 < 0.2.0`, `~beta.10 > ~beta.3`), with an old file date that must not matter;
  - `apt-get update`, `madison` and a rollback download with a real apt client;
  - by-hash;
  - republishing and its three refusals;
  - re-signing with byte-identical indexes;
  - `releases.json` and its signature;
  - `check` catching a changed index.
- **`split-iso.test.sh`, 17 passed** (new). The PowerShell join and check it generates also ran on Windows PowerShell 5.1 on the development machine.
- **`deb-version.test.sh` 40, `docker-archive.test.sh` 9, `image-apt-check.test.sh` 7 passed** after the tag rules moved into `common.sh`.
- **`samba-ucf-keeps-include.sh`, 12 passed** (new; Samba 4.22.11, ucf 3.0052).
- **Changed scripts re-run in fresh containers:**
  - all seven packages build lintian-clean;
  - `installed-smoke.sh` 49;
  - `maintscripts.test.sh` 82;
  - branding `install.test.sh` 92;
  - `apt-integration.test.sh` 6;
  - `catalog.test.sh` 32;
  - `vm-libvirt.sh` 7 (real libvirt).
- **The workflows:** checked against themselves (every `needs`, output, step id, matrix key and permission), and every action is pinned to its tag's commit (`tools/pin-actions.sh --check`).
- **On GitHub:** the first `ci.yml` run found three problems (NOTE-71), all fixed. Run 45230381774, for commit `af30c42`, then passed all 21 jobs on GitHub's runners. `release.yml` and `refresh.yml` haven't run yet; they need the signing key and the secrets (`docs/RELEASING.md`, one-time setup).

**os-release, unattended-upgrades, Docker and Calamares** (`os-identity.test.sh`, NimbusOS os-release installed). Excerpts:

```
$ grep ID/CODENAME /etc/os-release
NAME="NimbusOS"   ID=nimbusos   ID_LIKE=debian   VERSION_CODENAME=trixie   DEBIAN_CODENAME=trixie
$ cat /etc/debian_version            -> 13.7 (base-files: /etc/debian_version)
$ lsb_release -a
Distributor ID: NimbusOS   Description: NimbusOS 0.0.0+git…   Codename: trixie
$ unattended-upgrade --dry-run --debug     (ntfs-3g installed at 1:2022.10.3-5+deb13u2)
Allowed origins are: origin=Debian,codename=trixie,label=Debian-Security, origin=Debian,codename=trixie-security,label=Debian-Security
Packages that will be upgraded: libntfs-3g89t64 ntfs-3g zlib1g
$ apt-get -s dist-upgrade
Inst zlib1g [1:1.3.dfsg+really1.3.1-1+b1] (1:1.3.dfsg+really1.3.1-1+deb13u1 Debian-Security:13/stable-security [amd64])
Inst ntfs-3g [1:2022.10.3-5+deb13u2] (1:2022.10.3-5+deb13u3 Debian-Security:13/stable-security [amd64]) []
Inst libntfs-3g89t64 [1:2022.10.3-5+deb13u2] (1:2022.10.3-5+deb13u3 Debian-Security:13/stable-security [amd64])
$ nimbus-update check (NimbusOS repository not published yet)
nimbusos: unreachable (nikolajalbrechtsen.github.io)
$ docker.list (rendered docker.list.binary) and Docker's own codename lookup
deb [arch=amd64 signed-by=/usr/share/keyrings/docker-archive-keyring.gpg] https://download.docker.com/linux/debian trixie stable
$(. /etc/os-release && echo "$VERSION_CODENAME")  -> trixie
$ apt-cache policy docker-ce   -> Candidate: 5:29.10.0-1~debian.13~trixie (download.docker.com trixie/stable, 500)
$ Calamares 3.3.14 + calamares-settings-debian 13.0.13
helpers/calamares-sources-media: RELEASE="trixie"; helpers/calamares-sources-final: RELEASE="trixie"
(no Calamares program file or module mentions os-release or VERSION_CODENAME)
```

What this shows:
- **unattended-upgrades:** `${distro_codename}` still expands to `trixie`, and its allowed origins are exactly the two Debian-Security patterns, so a pending security update is picked up.
- **apt** sees the same updates from Debian-Security.
- **Docker:** its source and its documented codename lookup both resolve to trixie.
- **Calamares:** Debian's Calamares helpers hard-code the release and don't depend on os-release at all. NimbusOS replaces `sources-final` anyway (NOTE-06).

Facts confirmed by reading live-build `1:20250505+deb13u1`'s own scripts and Debian's `calamares-settings-debian 13.0.13`:
- `chroot_archives` copies `*.key.chroot` unchanged to `trusted.gpg.d/<name>.asc` (`.gpg` if the file isn't ASCII-armored; it tests with `grep "PGP PUBLIC KEY BLOCK"`), and removes both the `.list.chroot` source and that key itself at the end of the chroot stage (NOTE-03).
- `binary_package-lists` resolves each `.list.binary` separately against an empty dpkg status.
- `binary_grub_cfg` and `binary_syslinux` copy `config/bootloaders/{grub-pc,isolinux,syslinux_common}` over the built-in templates.
- `binary_bootloader_splash` renders `config/bootloaders/splash.svg` (640×480 ISOLINUX, 800×600 GRUB).
- Package lists go through `printf`/`read` (NOTE-53).
- Debian's `sources-final` writes only `main non-free-firmware` and adds backports (NOTE-06).
- Debian's `bootloader-config` installs `grub-efi` or `grub-pc`, plus `cryptsetup-initramfs cryptsetup keyutils` for encrypted installs, from the ISO pool.

---

## 8. Deliverable map

| # | Deliverable | Files |
|---|---|---|
| 1 | Plan | `docs/PLAN.md`, `.gitattributes`, `.gitignore` |
| 2 | Build entry point | `build/nimbus.conf`, `build/build.sh`, `build/lib/common.sh`, `tools/build-debs.sh`, `tools/deb-version.sh` |
| 3 | Package lists | `build/config/package-lists/*`, `tools/gen-desktop-depends.sh`, `tools/verify-trixie.sh`, `tools/dev-shell.sh`, `tools/tests/{deb-version,image-apt-check}.test.sh` |
| 4 | Build-time archives | `build/config/archives/{docker.list.chroot,docker.key.chroot,docker.list.binary}`, `tools/fetch-docker-key.sh`, `tools/tests/docker-archive.test.sh` |
| 5 | Debian packaging (7 packages), in three parts | **Part 1:** `packages/{nimbusos-archive-keyring,nimbusos-docker-keyring,nimbusos-release,nimbusos-desktop}/**` (os-release, source, pinning with the `apt-cache policy` proof, unattended-upgrades drop-ins, `nimbus-update`, timers, Flathub setup, tests), plus `build-debs.sh` token rendering, mode normalization and `--only`. **Part 2 (done):** `packages/nimbusos-branding/**` (all themes and art, CC-BY-SA-4.0 text from SPDX, install test). **Part 3 (done):** `packages/{nimbus-control,nimbus-catalog}/{debian,tests}/**`, plus the parts of `control-center/` the packaging runs: `systemd/*` (all units), `data/samba/nimbus.conf`, `bin/nimbus-migrate`, `migrations/0001_initial.py` and `tests/test_migrate.py` (NOTE-58 to NOTE-61) |
| 6 | Live hooks (done) | `build/config/hooks/live/{0100,0200,0300,0900}-*.hook.chroot`, `nimbus_check_image_initrd` in `build/lib/common.sh` (instead of a hook 9990, NOTE-62), `tools/tests/{hooks,image-initrd-check}.test.sh` |
| 7 | Live-only files and boot menus (done) | `build/config/includes.chroot_after_packages/**`, `build/config/bootloaders/**`, hook `0400-live-manifest`, the verify hook's installer checks, `--bootappend-live-failsafe` and `nimbus_prepare_grub_theme` in `build/lib/common.sh`, `tools/tests/{live-files,bootloaders}.test.sh` (NOTE-63, NOTE-64) |
| 8 | Nimbus Control Center, in five batches (done, NOTE-65) | **Batch 1 (done):** backend core (app, auth, errors, config, fonts, privd client, system, updates), `nimbus_common` (validate, protocol), privd (server, registry, runner, audit, updates, system), launcher and Grant access, desktop file and polkit action, the whole design system (`style.css`, `lib/{api,ui,icons}.js`, `app.js`), `views/updates.js`, tests. **Batch 2 (done, NOTE-66):** `backend/nimbus_control/{manifest,catalog,compose,apps_service,jobs,docker_api,state}.py`, privd `stacks.py`, the validators for apps, form components in `lib/ui.js`, `views/apps.js`, the fixture catalog and `test_{manifest,compose,apps}.py`, privd stack tests, Apps checks in `installed-smoke.sh`, Apps scenes in `ui_preview.py`. **Batch 3 (done, NOTE-67):** `backend/nimbus_control/{libvirt_api,vm_xml,vm_service,iso_library}.py`, the VM validators and settings, `views/vms.js`, `lib/vnc.js`, `console.{html,js}`, the CSP for websockify, `test_{vm_xml,isos,vms,vm_libvirt}.py`, `tests/integration/vm-libvirt.sh`, VM checks in `installed-smoke.sh`, VM scenes and `--live-vm` in `ui_preview.py`. **Batch 4 (done, NOTE-68):** privd `storage.py`, `samba.py`, `fsutil.py`, `nimbus_common/shares.py`, `backend/nimbus_control/{storage_service,shares_service}.py`, settings in `state.py`, `views/{storage,settings}.js`, `bin/{nimbus-netfix,nimbus-snapraid-run,nimbus-recycle-clean}`, the Samba drop-in's new sections, `test_{storage_privd,samba_privd,nas_api,nas_scripts}.py`, `tests/integration/nas-integration.sh`, NAS checks in `installed-smoke.sh`, storage scenes in `ui_preview.py`. **Batch 5 (done, NOTE-69):** `backend/nimbus_control/{metrics_service,docker_stream,alerts}.py`, `apps_service.diagnose`, libvirt's statistics call and state mapping, unprotected bytes in privd `storage.py`, `views/dashboard.js`, `lib/charts.js`, the console dialog in `lib/vnc.js`, navigation intents in `app.js`, `test_{metrics,alerts}.py`, `tests/integration/docker-streams.sh`, Dashboard checks in `installed-smoke.sh`, `vm-libvirt.sh` and `nas-integration.sh`, Dashboard scenes in `ui_preview.py`. Each batch adds its lines to `packages/nimbus-control/debian/install` (NOTE-61) |
| 9 | Catalog (done, NOTE-70) | `catalog/SCHEMA.md`, `catalog/manifest.schema.json` (with `template_version`, NOTE-60), the 10 apps; `webui.scheme`, `single_instance` and `companions` in `manifest.py`, `apps_service.py`, `docker_api.py`, `metrics_service.py` and the Catalog card; `timezone` and `share_gid` for templates; `check-catalog.py` refuses extra files; `test_catalog.py`, `tests/integration/catalog-images.sh`, catalog checks in `installed-smoke.sh` and `catalog.test.sh`, the real catalog in `ui_preview.py` |
| 10 | CI/CD and docs (done, NOTE-71) | `.github/workflows/{ci,release,refresh}.yml`, `.github/dependabot.yml`, `tools/{publish-apt.sh,releases-json.py,split-iso.sh,pin-actions.sh,import-signing-key.sh,lint.sh}`, `tools/tests/{publish-apt,split-iso}.test.sh`, the tag rules in `build/lib/common.sh`, `control-center/tests/integration/samba-ucf-keeps-include.sh`, `README.md`, `LICENSE`, `LICENSES/CC-BY-SA-4.0.txt`, `.shellcheckrc`, `ruff.toml` (and the lint fixes it found), `docs/{RELEASING,SIGNING}.md`, `docs/release-notes/v0.1.0.md` |
| 11 | Testing (the smoke test includes `systemctl --user show-environment \| grep XDG_CONFIG_DIRS` and kinfocenter's About page after a graphical login, NOTE-55; the Dashboard's Docker and VM numbers against `docker stats` and `virsh domstats`, and SnapRAID move lines on real disks, NOTE-69) | `docs/TESTING.md`, `tools/qemu-test.sh` |

---

## 9. Decisions from review (2026-11-17)

1. **Maintainer.** Your name and address as set in `NIMBUS_MAINTAINER` (`build/nimbus.conf`), the only place it is written. It reaches `debian/control` and the changelog trailers from there, and stays out of commit trailers, code comments, docs and test data (A11). This replaced the earlier no-reply placeholder; the placeholder warning and the planned release-workflow block were removed.
2. **License.** GPL-3.0-or-later for code and packaging, CC-BY-SA-4.0 for artwork, DEP-5 `debian/copyright`, fonts left in Debian's packages (A12, NOTE-13). The CC-BY-SA-4.0 text comes from SPDX's `license-list-data` (`text/CC-BY-SA-4.0.txt`), pinned to a commit that is noted in `debian/copyright` (deliverable 5).
3. **Network.** Allowed in deliverables 4 and 10. In deliverable 4, Docker's key is committed only after its fingerprint matches, the fingerprint is written into a comment, and CI re-checks it. In deliverable 10, actions are pinned to released-tag SHAs with a tag comment, and Dependabot is set up. Both get re-runnable fetch-and-verify scripts. `verify-trixie.sh` was also run against deb.debian.org in deliverable 3 (§7).
4. **Review changes.** NOTE-03 (no hand-run stages; one Docker source), NOTE-04 (repository check in `build.sh`), NOTE-14 (code expires after first use or 30 s), NOTE-23 (ucf CI test), NOTE-26 (hide "Pooled + parity" without snapraid), NOTE-34 (locks, random delay, never reboot), NOTE-35 (journal), NOTE-41 (xz, sums for parts and ISO, PowerShell join), NOTE-43 (ruff on the runner only).
5. **Docker keyring package.** Docker's key ships in its own package, `nimbusos-docker-keyring`, which `nimbusos-desktop` depends on. That makes seven packages. `build.sh` checks the finished image's APT setup (§2, step 6).
6. **Test environment.** `tools/dev-shell.sh` runs a `debian:trixie` container with Docker or Podman. The version-mapping tests pass there with real `dpkg` 1.22.22: 40 of 40 checks.
7. **APT repository and Docker key rotation** (before deliverable 5). `apt-ftparchive` replaces `reprepro`, with the requirements in NOTE-52. A weekly job runs `fetch-docker-key.sh --check`; on a change it fails and opens one issue, and it never updates the key (NOTE-05). The maintainer's address appears only in the `Maintainer` field and `debian/changelog` (A11, NOTE-49).
8. **Themes through Debian's alternatives** (before deliverable 5, part 3). NimbusOS is desktop-base's `desktop-theme` at priority 80, and every desktop-base background, login and GRUB alternative follows it. Plymouth and GRUB regeneration keep their direct handling, because Trixie has neither a Plymouth alternative nor an `update-grub` trigger (NOTE-56). NOTE-55's session path is confirmed, and it gets a check in the deliverable 11 smoke test.
9. **Rules for nimbus-control and nimbus-catalog** (deliverable 5, part 3):
   - accounts from sysusers.d and directories from tmpfiles.d;
   - purge never deletes user data and says what it kept (NOTE-58);
   - maintainer scripts work in the image build chroot, start and restart only through `deb-systemd-invoke`, and never call docker, virsh or smbcontrol (NOTE-61);
   - state migrations run step by step through `schema_version`, with a backup first and one clear journal message on failure (NOTE-59);
   - each app manifest has its own `template_version` (NOTE-60).
10. **No hook 9990** (deliverable 6). live-build already regenerates the initramfs after all hooks, so `build.sh` checks that the ISO's initramfs carries the NimbusOS boot splash instead (NOTE-62).
11. **Installer minimums** (deliverable 7): 32 GB of disk and 4 GB of memory in Calamares' welcome check. This is a policy choice; deliverable 11 checks it against the installed size (NOTE-63).
12. **Control Center in five batches** (deliverable 8): the foundation plus Updates first, then Apps, VMs, Storage & Shares with Settings, and the Dashboard last because it shows all of them (NOTE-65).
