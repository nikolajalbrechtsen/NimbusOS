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
├── LICENSES/                         GPL-3.0-or-later.txt, CC-BY-SA-4.0.txt (artwork), fetched verbatim (deliverable 10)
├── .gitattributes                    LF line endings everywhere (NOTE-50)
├── .gitignore                        build products, caches, anything that looks like a private key
├── .shellcheckrc                     repo-wide shellcheck settings
├── ruff.toml                         ruff settings for all Python in the repo
│
├── .github/dependabot.yml            keeps the SHA-pinned GitHub Actions current (NOTE-42)
├── .github/workflows/
│   ├── ci.yml                        push + PR: shellcheck, ruff, pytest, verify-trixie, Samba/ucf test, build all .debs
│   ├── release.yml                   tags vX.Y.Z / vX.Y.Z-beta.N: debs → APT → ISO → GitHub Release
│   └── refresh.yml                   weekly: re-sign Release files; bootstraps an empty repo once (NOTE-04)
│
├── build/
│   ├── nimbus.conf                   every build tunable, one place (names, URLs, mirrors, live user, split size…)
│   ├── build.sh                      host checks → repo check → .debs → lb clean / lb config / lb build → ISO + .sha256
│   ├── lib/common.sh                 logging, nimbus.conf loading, version helpers (shared with tools/)
│   └── config/                       live-build config TEMPLATE; copied to build/work/ for each build
│       ├── package-lists/
│       │   ├── 10-base.list.chroot
│       │   ├── 20-plasma.list.chroot
│       │   ├── 30-theming-fonts.list.chroot
│       │   ├── 40-calamares.list.chroot             marked exclude-from-desktop-depends (NOTE-48)
│       │   ├── 50-docker.list.chroot
│       │   ├── 60-virtualization.list.chroot
│       │   ├── 70-storage-nas.list.chroot
│       │   ├── 90-nimbusos.list.chroot              nimbusos-desktop itself (marked exclude-from-desktop-depends)
│       │   ├── 95-installer-pool-bios.list.binary   grub-pc → ISO pool for Calamares' bootloader step
│       │   ├── 96-installer-pool-uefi.list.binary   grub-efi + signed GRUB/shim → ISO pool
│       │   └── 97-installer-pool-luks.list.binary   cryptsetup-initramfs, keyutils → ISO pool (encrypted installs)
│       ├── archives/                                Docker CE source (NOTE-03, NOTE-05)
│       │   ├── docker.list.chroot                   build-time source, trusted through docker.key.chroot (no signed-by)
│       │   ├── docker.key.chroot                    Docker's public key, byte-identical to Docker's download
│       │   └── docker.list.binary                   the image's source: signed-by=/usr/share/keyrings/docker-archive-keyring.gpg
│       │                                            (both lists state the fingerprint in a comment; CI checks it)
│       ├── hooks/live/
│       │   ├── 0100-flathub-preseed.hook.chroot     runs the packaged Flathub setup once at build time
│       │   ├── 0200-live-installer-launcher.hook.chroot  hides Debian's "Install Debian" entry in the live image
│       │   ├── 0300-live-units.hook.chroot          enables the live-only units below
│       │   ├── 0900-verify-image.hook.chroot        fails the build if groups/services/os-release are wrong
│       │   └── 9990-update-initramfs.hook.chroot    final initramfs with the NimbusOS Plymouth theme
│       ├── includes.chroot_after_packages/          live-session-only files (NOTE-02, NOTE-06)
│       │   ├── etc/calamares/settings.conf
│       │   ├── etc/calamares/modules/{users,bootloader,welcome,finished}.conf
│       │   ├── etc/calamares/modules/shellprocess_nimbus-sources-final.conf
│       │   ├── etc/calamares/modules/shellprocess_nimbus-target-cleanup.conf
│       │   ├── etc/systemd/system/var-lib-docker.mount   tmpfs for Docker in the live session only (NOTE-21)
│       │   ├── etc/xdg/autostart/nimbus-live-session.desktop
│       │   ├── usr/lib/nimbusos-live/{sources-final,target-cleanup,live-session-setup}
│       │   └── usr/share/applications/nimbus-install.desktop   "Install NimbusOS"
│       └── bootloaders/                  overlays; live-build copies them over its own templates itself
│           ├── splash.svg                one splash for both menus, rendered by live-build in the chroot (NOTE-12)
│           ├── grub-pc/                  GRUB (EFI) live menu: labels + theme.cfg; live-theme/ is filled from nimbusos-branding
│           └── isolinux/                 ISOLINUX menu: stdmenu.cfg, labels
│
├── packages/                         one Debian source package per directory: debhelper-compat (= 13), 3.0 (native)
│   ├── nimbusos-archive-keyring/     keys/*.asc → dearmored NimbusOS keyring
│   ├── nimbusos-docker-keyring/      Docker's key → /usr/share/keyrings/docker-archive-keyring.gpg (NOTE-05)
│   ├── nimbusos-release/             os-release.in, apt/*, nimbus-update (CLI + man page), systemd/*
│   ├── nimbusos-branding/            art/*.svg, grub/, plymouth/, sddm/, calamares/, plasma/, xdg/, Makefile
│   ├── nimbus-control/               debian/ only; payload staged from control-center/ at build time
│   ├── nimbus-catalog/               debian/ only; payload staged from catalog/ at build time
│   └── nimbusos-desktop/             metapackage; Depends generated from build/config/package-lists/
│
├── control-center/
│   ├── backend/nimbus_control/       FastAPI: app.py, auth.py, metrics_service.py, docker_service.py,
│   │                                 vm_service.py, storage_service.py, shares_service.py,
│   │                                 updates_service.py, privd_client.py, state.py, migrations/
│   ├── common/nimbus_common/         shared by backend and privd: validators, smb.conf renderer, schemas
│   ├── privd/                        nimbus-privd.py + nimbus_privd/ (disks, pool, samba, smbusers, updates, audit)
│   ├── frontend/                     index.html, console.html, style.css, app.js,
│   │                                 lib/{api,ui,icons,charts}.js,
│   │                                 views/{dashboard,apps,vms,storage,updates,settings}.js
│   ├── bin/                          nimbus-control-server, nimbus-control-open (launcher), nimbus-migrate,
│   │                                 nimbus-snapraid-run, nimbus-recycle-clean, nimbus-netfix, nimbus-grant-access
│   ├── systemd/                      nimbus-control.service, nimbus-privd.{socket,service},
│   │                                 nimbus-websockify.service, nimbus-snapraid-sync.{service,timer},
│   │                                 nimbus-recycle-clean.{service,timer}, nimbus-netfix.service
│   ├── data/                         samba drop-in, polkit action, nimbus-control.desktop, icons
│   └── tests/                        pytest; no network, no root; Docker/libvirt/privd are faked
│       └── integration/samba-ucf-keeps-include.sh   CI: a ucf-managed smb.conf update keeps the include (NOTE-23)
│
├── catalog/
│   ├── SCHEMA.md                     manifest schema, documented field by field
│   ├── manifest.schema.json          the same schema as JSON Schema (checked in CI and at runtime)
│   └── <app-id>/{manifest.json, docker-compose.yml.j2, icon.svg}
│        portainer, uptime-kuma, vaultwarden, nextcloud-aio, jellyfin,
│        pihole, syncthing, home-assistant, grafana, nginx-proxy-manager
│
├── apt/                              repository config for gh-pages: stable + beta, last 3 versions, Valid-Until 14 d (NOTE-52)
│
├── tools/
│   ├── build-debs.sh                 stage + build all 7 .debs (used by build.sh, ci.yml, release.yml)
│   ├── deb-version.sh                git tag → Debian version (NOTE-32)
│   ├── gen-desktop-depends.sh        package lists → ${nimbus:ListDepends} substvar (NOTE-48)
│   ├── verify-trixie.sh              proves every listed package and live-build flag exists in trixie (§7)
│   ├── dev-shell.sh                  throwaway debian:trixie container (Docker or Podman) with the repo at /src
│   ├── fetch-docker-key.sh           re-downloads Docker's key and refuses it unless the fingerprint matches
│   ├── pin-actions.sh                resolves each action's release tag to its commit SHA (NOTE-42)
│   ├── publish-apt.sh                add packages, prune to 3 versions, index, releases.json, sign (release + refresh)
│   ├── releases-json.py              maintains apt/releases.json
│   ├── split-iso.sh                  splits ISOs over the GitHub asset limit; writes join instructions
│   ├── qemu-test.sh                  the UEFI/BIOS QEMU test commands from docs/TESTING.md
│   └── tests/
│       ├── deb-version.test.sh       version mapping (real dpkg; 40 checks)
│       ├── image-apt-check.test.sh   final-image APT check: good image + 6 failure modes
│       └── docker-archive.test.sh    fetch-docker-key.sh --verify-local: real files + 8 breakages
│
└── docs/
    ├── PLAN.md                       this file
    ├── RELEASING.md                  one-time GitHub setup, stable/beta releases, rollback
    ├── SIGNING.md                    offline ed25519 key, secrets, key rotation
    ├── TESTING.md                    QEMU commands + smoke-test checklist
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
6. Checks the finished image's APT setup by reading `filesystem.squashfs` with `unsquashfs`. The image must contain `/etc/apt/sources.list.d/docker.list` from `docker.list.binary`, with every line `signed-by=/usr/share/keyrings/docker-archive-keyring.gpg`. It must contain no trace of `docker.list.chroot`, no second Docker source, no Docker key in `trusted.gpg.d`, and the packaged keyring must hold exactly Docker's key. Then it renames the output to `nimbusos-amd64.iso`, writes `nimbusos-amd64.iso.sha256`, and says whether the ISO is over GitHub's 2 GiB asset limit (release.yml then splits it, NOTE-41).

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
| `nimbusos-branding` | GRUB theme + `/etc/default/grub.d/10-nimbusos.cfg`, Plymouth theme, SDDM theme + `/etc/sddm.conf.d/10-nimbusos.conf`, Calamares branding, wallpaper, Plasma global theme `org.nimbusos.desktop` (with `layout.js`), `NimbusDark.colors`, `/etc/xdg` defaults, logo icons | Depends: `papirus-icon-theme`, `fonts-inter`, `fonts-jetbrains-mono`, `plymouth`, `plymouth-label` | postinst: register `default.plymouth` alternative; `update-grub` if GRUB is installed; initramfs trigger |
| `nimbus-control` | backend, frontend, `nimbus-privd`, units, Samba drop-in, polkit action, launcher | Depends: the Python stack (`python3-fastapi`, `python3-uvicorn`, `python3-websockets`, `python3-docker`, `python3-libvirt`, `python3-psutil`, `python3-jinja2`, `python3-yaml`, `python3-jsonschema`, `python3-systemd`), `fonts-inter`, `fonts-jetbrains-mono` (served to the UI from their own paths, so the OFL license stays with Debian's packages), `docker-ce`, `docker-compose-plugin`, `libvirt-daemon-system`, `samba`, `wsdd2`, `avahi-daemon`, `mergerfs`, `novnc`, `websockify`, `chromium`, …; Recommends: `snapraid` (NOTE-26) | Creates the groups `nimbus-control` and `nimbus-smb`, the system users `nimbus-control` and `nimbus-guest` (NOTE-25), and adds `nimbus-control` to `docker`, `libvirt` and `nimbus-smb`. Runs schema migrations step by step. Adds the smb.conf include (NOTE-23) and the `adduser.conf` keys (NOTE-09). Enables services on first install only. Restarts its own services only when their files changed |
| `nimbus-catalog` | `/usr/share/nimbus-control/catalog/<app-id>/` | Enhances: `nimbus-control` | none. The backend notices catalog changes on its own and marks installed stacks with "Template update available" |
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
| `/usr/lib/nimbus-control/` | root | backend, `nimbus_common`, privd (`nimbus-privd.py`), `www/`, `bin/` |
| `/usr/share/nimbus-control/catalog/` | root | catalog (`nimbus-catalog`) |
| `/var/lib/nimbus-control/` | `nimbus-control` 0750 | `schema_version`, `settings.json`, `shares.json`, `stacks.json`, `stacks/<stack>/` |
| `/var/lib/nimbus-vms/{isos,images}/` | isos: `nimbus-control`; images: libvirt pool `nimbus-images` | ISO library (NOTE-27), qcow2 disks |
| `/var/lib/nimbus-privd/` | root 0700 | SMB user registry, SMART/parity cache, fstab backups |
| `/run/nimbus-control/token` | `nimbus-control:nimbus-control` 0640 | per-boot API token (tmpfs, so it is new every boot) |
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
- **Alert rules** (all in `metrics_service.py`): container exited unexpectedly (its stack's desired state is "running"), restart loop (≥ 3 restarts in 5 min, or `Restarting`), unhealthy; VM crashed or paused with reason I/O error; SMART failing, reallocated or pending sectors > 0, temperature over threshold (HDD 50 °C, NVMe 70 °C by default); pool > 90 % full; parity not synced for more than 48 h or more than 10 GB unprotected; a failed unit (`nimbus-*`, `smbd`, `docker`, the libvirt daemons, NOTE-20), checked every 10 s; security updates waiting, a new NimbusOS release, restart required, repository unreachable, or not checked for more than 7 days.

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
- **`--check`** (CI, network) compares the committed key material with what Docker serves now, so a key rotation is noticed early.

`build.sh` and `tools/build-debs.sh` check the fingerprint before every build. Both sources carry `arch=amd64`, so adding a foreign architecture later (for example i386) doesn't make apt look for Docker packages Docker doesn't build. `nimbusos-release` ships `nimbusos-docker.pref`, which pins the Docker origin the same way as NimbusOS's: priority 1 for everything, 500 for `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin`, `docker-buildx-plugin` and `docker-ce-rootless-extras`. Docker's repository therefore cannot replace Debian packages.

**NOTE-06 — Calamares configuration is live-only.** Calamares runs only in the live session, and its own `packages` step purges it from the installed system. `settings.conf` and the module overrides therefore live in `includes.chroot_after_packages/`, where they replace `calamares-settings-debian`'s copies inside the live image. The branding component `/usr/share/calamares/branding/nimbusos/` ships in `nimbusos-branding`, as specified. Debian's `bootloader-config` and `sources-media` steps are kept. Debian's `sources-final` is replaced by `nimbus-sources-final`, which writes deb822 `/etc/apt/sources.list.d/debian.sources` with `main contrib non-free non-free-firmware` for `trixie`, `trixie-updates` and `trixie-security`, and empties the legacy `sources.list`. The NimbusOS and Docker sources are already on the target, because `nimbusos-release` is part of the copied image. A final `nimbus-target-cleanup` step deletes the live-only files from the target. `users.conf` uses the Calamares 3.3 `hostname:` block with `template: "nimbusos"`.

**NOTE-07 — Secure Boot and the EFI directory.** Installed systems boot through Debian's signed shim and GRUB, which NimbusOS does not rebuild, so Secure Boot keeps working. `/etc/default/grub.d/10-nimbusos.cfg` sets `GRUB_DISTRIBUTOR=NimbusOS`. That makes the boot entries read "NimbusOS" and makes `grub-install` use `EFI/nimbusos/`. Calamares' `efiBootloaderId` is set to the same value, so the installer and later GRUB package upgrades agree, and `installEFIFallback: true` also writes `EFI/BOOT/BOOTX64.EFI`. This depends on assumption A6. If smoke test 1 fails with Secure Boot on, the fallback is `GRUB_DISTRIBUTOR=Debian`: the EFI directory stays `EFI/debian`, but boot entries then read "Debian GNU/Linux".

**NOTE-08 — The SDDM greeter stays on X11.** SDDM's Wayland greeter is less dependable in VMs and on some GPUs, so the login screen uses Debian's default X11 greeter with the NimbusOS QML theme. The **session** defaults to Plasma (Wayland): the live autologin uses `Session=plasma`, and new installs get SDDM's last-session state pre-set to `plasma.desktop`. Plasma (X11) remains selectable.

**NOTE-09 — `/etc/adduser.conf`.** The file belongs to the `adduser` package and has no drop-in directory. The `nimbus-control` postinst therefore sets exactly two keys in place, `EXTRA_GROUPS="docker libvirt kvm nimbus-control"` and `ADD_EXTRA_GROUPS=1`. It does this idempotently and only when they differ. This is a deliberate, documented exception to Debian Policy §10.7.4. `EXTRA_GROUPS` only applies to accounts made with `adduser`. Plasma's Users settings page creates accounts through AccountsService and `useradd`, which ignore it. For such users the launcher detects the missing group and offers **Grant access**, which asks for an administrator password through polkit; the user then logs out and back in.

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

**NOTE-20 — libvirt daemons.** Trixie's `libvirt-daemon-system` may use the modular daemons (`virtqemud`, `virtnetworkd`, `virtstoraged`, socket-activated) instead of the monolithic `libvirtd`. The postinst enables whichever the installed version provides, and the service-health alert checks the same units. The `default` NAT network is set to autostart on first install by creating `/etc/libvirt/qemu/networks/autostart/default.xml` (a symlink to `../default.xml`), which needs no running daemon.

**NOTE-21 — The live session.** The live root filesystem is an overlay, which Docker's `overlay2` storage driver cannot sit on. A live-only mount unit therefore puts `/var/lib/docker` on tmpfs: apps work but are gone at shutdown. Disk formatting and pool changes are disabled behind the banner "Install NimbusOS to set up storage"; read-only views work.

**NOTE-22 — No firewall.** NimbusOS ships without an active firewall. If you enable one, allow SMB with `ufw allow 445/tcp`, or for firewalld `firewall-cmd --permanent --add-service=samba && firewall-cmd --reload`. Settings shows the same text.

### Storage and Samba

**NOTE-23 — A single include line in `smb.conf`.** Debian's `smb.conf` has no include directory, and Samba reads nothing else. The `nimbus-control` postinst appends one marked block to the end of `/etc/samba/smb.conf`: `include = /etc/samba/nimbus/nimbus.conf`. Everything else NimbusOS sets lives in `/etc/samba/nimbus/` (static globals as a conffile; generated `settings.conf`) and in `/etc/samba/nimbus-shares.conf`. Debian's ucf handling of `smb.conf` otherwise stays as it is. The updater sets `UCF_FORCE_CONFFOLD=1` so a Samba upgrade doesn't replace the file. A CI test (`control-center/tests/integration/samba-ucf-keeps-include.sh`, in `debian:trixie`) checks that this works. It installs `samba`, appends the include, changes the packaged template, runs the same ucf update a Samba upgrade runs (with `--force-confold` / `UCF_FORCE_CONFFOLD=1`), and fails if the include line is gone. privd's repair is only the fallback: it checks for the include before each `share.apply` and on startup, puts it back if it is missing, and raises an alert when it does.

**NOTE-24 — Samba's own mDNS registration is turned off.** `multicast dns register = no` stops smbd from advertising itself through Avahi. The generated `/etc/avahi/services/smb.service` then becomes the only advertisement, which avoids duplicate entries in Finder and lets NimbusOS add the `_adisk` record for Time Machine.

**NOTE-25 — A dedicated guest account.** Public shares should be writable by guests without world-writable folders. The package therefore creates a `nimbus-guest` system user (nologin, no home, member of `nimbus-smb`) and sets it as Samba's `guest account`. `map to guest = Bad User` is set only while at least one Public share exists.

**NOTE-26 — SnapRAID.** `/etc/snapraid.conf` may belong to Debian's snapraid package, so NimbusOS writes `/etc/nimbus/snapraid.conf` and always calls `snapraid -c`. Parity is **scheduled, not real time**: nightly sync, and a weekly scrub of 8 % of the array covering blocks older than 10 days. The pool card and the wizard both say so. privd refreshes SMART data hourly with `smartctl -n standby`, so spun-down disks are not woken. Availability: confirmed. `snapraid` 12.4-1 is in Trixie main (§7), so the planned fallback package is not needed. `nimbus-control` still only Recommends `snapraid`. Whenever `/usr/bin/snapraid` is missing, the backend reports no parity support, and the NAS wizard and pool settings hide "Pooled + parity" instead of offering it and then failing.

**NOTE-27 — The ISO library when an `isos` share exists.** The VM tab always uses `/var/lib/nimbus-vms/isos`. When an `isos` share exists, privd bind-mounts the share's folder onto that path, through a managed fstab line. `nimbus-control` then keeps one narrow `ReadWritePaths=` entry, and ISOs dropped in over SMB show up in the VM wizard.

### Catalog

**NOTE-28 — Catalog icons.** Each app gets a neutral monogram SVG (initials on the app's category color), not the vendor's logo. This avoids trademark problems and binary files.

**NOTE-29 — Nextcloud AIO and Vaultwarden are exceptions.** Nextcloud AIO requires the container name `nextcloud-aio-mastercontainer`, the named volume `nextcloud_aio_mastercontainer` (its data is not under the stack directory) and the Docker socket. It also starts its own `nextcloud-aio-*` containers, which the dashboard groups under the AIO app. Vaultwarden's web vault needs a secure context, so its manifest sets `webui.secure_context: true` and its Open link uses `http://localhost:<port>` instead of the hostname. For LAN access, the notes point to Nginx Proxy Manager with TLS.

**NOTE-30 — Compose templates and the raw editor.** Every value inserted into a `.j2` template goes through a JSON-quoting filter (`| q`) in a Jinja2 `SandboxedEnvironment` with `StrictUndefined`. That rules out YAML injection. In the raw editor, a bind mount must resolve inside the stack directory, inside a share, or to one of the host paths listed in the app's manifest (for example `/var/run/docker.sock` for Portainer and AIO, `/run/dbus` for Home Assistant). Anything else is rejected with a message that says which path broke the rule.

**NOTE-31 — Stack data owned by container users.** Containers write their data as root or as their own UID (Grafana uses 472), which `nimbus-control` cannot change or delete. `stack.prepare_volume` (chown to the UID in the manifest) and `stack.delete_data` are privd verbs, limited to paths inside `/var/lib/nimbus-control/stacks/<stack>/`.

### Updates and releases

**NOTE-32 — Beta tags map to `~beta` versions.** A tag `vX.Y.Z-beta.N` becomes Debian version `X.Y.Z~beta.N`, so the final `X.Y.Z` sorts above all of its betas. (`X.Y.Z-beta.N` would be an invalid native version, and Debian would sort it above `X.Y.Z`.)

**NOTE-33 — Switching from Beta back to Stable doesn't downgrade.** Beta adds the `beta` suite on top of `stable`, both at priority 500, so apt picks the highest version. Switching back to Stable removes the `beta` suite, and the system keeps its beta versions until Stable passes them. The confirmation dialog says this, and `docs/RELEASING.md` shows an explicit downgrade with `apt install <pkg>=<version>`.

**NOTE-34 — unattended-upgrades installs security updates only.** Debian's default unattended-upgrades configuration also installs regular point-release updates. The NimbusOS drop-in (`52nimbusos-unattended-upgrades`) uses apt's `#clear Unattended-Upgrade::Origins-Pattern;` and then re-adds only the Debian-Security origins, and keeps `Automatic-Reboot "false"`. NimbusOS updates are **not** routed through unattended-upgrades, because it refuses to install new dependencies from origins outside its allowlist, and new Debian dependencies of `nimbusos-desktop` are how new software reaches existing installs. Instead, `nimbus-update-auto.timer` runs `nimbus-update auto` daily under the **Automatic** policy (`apt-get install --no-remove` of the installed `nimbus-*`/`nimbusos-*` packages, which pulls in new dependencies). The timer uses `RandomizedDelaySec=` so machines don't all hit the repository at once. `nimbus-update auto` first tries, without waiting, to take the same locks unattended-upgrades uses: unattended-upgrades' own lock file, then dpkg's `lock-frontend` and the APT lists lock, through python3-apt's `apt_pkg.get_lock`. If any is held, it logs "busy, retrying later" and exits. The service retries 30 minutes later, up to 3 times per day. It never reboots; it only sets the "restart required" state. **Notify only** runs just the daily check. **Off** runs neither, though Check now still works. Regular Debian updates arrive through **Install updates** or Discover.

**NOTE-35 — Updates run in a transient unit.** Upgrading `nimbus-control` restarts `nimbus-privd`. If apt were a child of privd, that restart would kill dpkg midway. privd therefore starts `nimbus-update install` with `systemd-run` as its own unit (`nimbus-update-<id>`, `SyslogIdentifier=nimbus-update`). All of its output goes to the journal. privd follows it with python3-systemd's journal reader and relays the lines to the UI. After a reconnect or page reload, the Updates page asks privd (`update.log`) for the unit's journal entries and shows the whole log again, finished or not.

**NOTE-36 — os-release.** `base-files` owns `/usr/lib/os-release`, so `nimbusos-release` diverts it (`dpkg-divert --rename`) and ships its own: `NAME="NimbusOS"`, `ID=nimbusos`, `ID_LIKE=debian`, `VERSION_ID`, `VERSION`, and `NIMBUSOS_CHANNEL`, a vendor-prefixed custom field, which os-release(5) allows. It keeps `VERSION_CODENAME=trixie`, because unattended-upgrades, Docker and other tools look up the release by codename. `/etc/os-release` stays base-files' symlink and therefore points to the NimbusOS file.

**NOTE-37 — Where release notes come from.** The spec takes release notes from the GitHub Release body, but the release is created in the last step, after `releases.json` has been published. The single source is therefore `docs/release-notes/<tag>.md`, written before tagging. `release.yml` uses that file both as the GitHub Release body and as the notes in `releases.json`, and fails with a clear message if it is missing. A line `Restart-Required: yes` in the file sets `restart_required`.

**NOTE-38 — `releases.json` is signed.** `releases.json` gets a detached signature, `releases.json.sig`, made with the archive key, and `nimbus-update` verifies it with `gpgv` against the NimbusOS keyring before showing any release notes. The notes are therefore as trustworthy as the packages.

**NOTE-39 — Pages is published from a branch.** GitHub Pages serves the `gh-pages` branch, so the workflows need only `contents: write`. `pages: write` and `id-token: write` belong to the artifact-based `deploy-pages` method, which isn't used. Each publish force-pushes `gh-pages` as a single fresh commit, which keeps the repository from growing with every release. The repository's state (the pool with the last 3 versions of each package) lives in the branch itself; it is public and contains no secrets.

**NOTE-40 — Scheduled workflows are paused after 60 days of inactivity.** GitHub disables scheduled workflows in public repositories that see no activity for 60 days. If `refresh.yml` stops, `InRelease` expires 14 days after its last signing, and installed systems show the "expired metadata" error, which explains how to fix it. `docs/RELEASING.md` explains how to re-enable the workflow.

**NOTE-41 — ISOs over 2 GiB are split.** A Plasma desktop with Docker, QEMU/libvirt, Chromium, Firefox and firmware is expected to be roughly 3.5–4.5 GB even with xz-compressed squashfs (`lb config --chroot-squashfs-compression-type xz`, set in `nimbus.conf`). That is above GitHub's 2 GiB limit per release asset. `tools/split-iso.sh` splits the ISO into 1,900 MiB parts (`nimbusos-amd64.iso.part00`, `.part01`, …). `SHA256SUMS` (and its signature `SHA256SUMS.gpg`) lists both the joined ISO and every part, so downloads can be checked before and after joining. The release body and README give join and verify commands for Linux/macOS (`cat nimbusos-amd64.iso.part* > nimbusos-amd64.iso`, `sha256sum -c --ignore-missing SHA256SUMS`) and for Windows PowerShell (a short stream-copy loop over the sorted parts, then `Get-FileHash -Algorithm SHA256`). An ISO under the limit is uploaded whole.

### CI and repository

**NOTE-42 — Actions are pinned to commit SHAs.** Workflows use only GitHub's own actions (`actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`). Each is pinned to the full commit SHA of a released tag, with the tag in a trailing comment (`# v4.2.2`). Releases are created with the preinstalled `gh` CLI, and no marketplace actions are used. The SHAs are resolved over the network in deliverable 10 by `tools/pin-actions.sh`, which you can re-run later. `.github/dependabot.yml` (ecosystem `github-actions`) opens PRs that move the pins and their comments forward.

**NOTE-43 — ruff is a CI tool only.** ruff is installed on the GitHub runner with `pipx install ruff==<pinned>`; pipx comes preinstalled on GitHub's Ubuntu runners. It is not part of the image or of any package, and `tools/verify-trixie.sh` doesn't check for it.

**NOTE-44 — Bootstrapping the signing key.** The repository can only hold the public key after you have generated it (`docs/SIGNING.md`). Until `packages/nimbusos-archive-keyring/keys/` contains a key, `ci.yml` builds the packages with a throwaway key generated inside the job and prints a warning. `release.yml` refuses to run without a committed public key and checks that its fingerprint matches the `APT_SIGNING_KEY` secret.

**NOTE-45 — Desktop-user groups and the `docker` group.** As specified, desktop users (live, Calamares and `adduser`) join `docker`, `libvirt`, `kvm` and `nimbus-control`. Membership in `docker` or `nimbus-control` is effectively root. `README.md` says this plainly, and the Control Center is meant for the machine's administrator.

**NOTE-46 — README location.** `README.md` sits at the repository root, which is what GitHub shows on the project page. `docs/` holds `RELEASING.md`, `SIGNING.md` and `TESTING.md`.

**NOTE-47 — Package build layout.** `packages/nimbus-control` and `packages/nimbus-catalog` contain only `debian/`. `tools/build-debs.sh` stages each package into `_build/src/<pkg>/`, copying `control-center/` or `catalog/` alongside, and builds there. The source trees stay where the spec puts them, and each package still builds from a self-contained directory.

**NOTE-48 — Which lists feed `nimbusos-desktop`.** `nimbusos-desktop` gets its Depends from the package lists at build time, through a substvar, so the lists and the metapackage can't drift apart. Lists containing the line `#! nimbus: exclude-from-desktop-depends` are skipped: Calamares, which removes itself after install (if the metapackage depended on it, removing Calamares would remove the metapackage), and `90-nimbusos` (the metapackage itself). The `*.list.binary` files are never read: they only fill the ISO's package pool, where GRUB for BIOS and for UEFI sit in separate lists because the two conflict and the installer picks one. A package listed twice is an error. The marker line is an ordinary comment as far as live-build is concerned (NOTE-53).

**NOTE-49 — `nimbus.conf` values reach the packages.** Build tunables that installed systems need (the APT URL, the `releases.json` URL, the Maintainer) are substituted from `build/nimbus.conf` into package templates at build time, so `nimbus.conf` remains the one place to change them.

**NOTE-50 — Developing on Windows.** The repository is edited on Windows with `core.filemode=false`. `.gitattributes` forces LF line endings, and executable bits are recorded explicitly with `git update-index --chmod=+x` for every script, hook and `debian/rules`. Everything that needs Debian runs in `tools/dev-shell.sh`. On this workstation that is Podman inside the WSL distribution `nimbus-build`; any Docker or Podman host works.

### Found by the Trixie archive check (deliverable 3)

**NOTE-51 — `wsdd2` instead of `wsdd`.** Trixie has no `wsdd` package (§7). `wsdd2` (1.8.7, main) serves the same purpose: it makes the machine show up under Network in Windows Explorer through WS-Discovery. The storage list, the `nimbus-control` Depends, the services table, the alert rules and the Settings page all use `wsdd2` and its unit, `wsdd2.service`.

**NOTE-52 — The APT repository can't be built with Trixie's `reprepro`.** Debian rolled `reprepro` back to 5.3.1 (`5.4.6+really5.3.2`). That version has `ValidFor:` but not `Limit:`, so a suite can hold only one version of each package. That breaks "keep the last 3 versions so users can roll back with `apt install <pkg>=<version>`". The closest supported alternative, used in deliverable 10, is `apt-ftparchive` from Debian's `apt-utils`, driven by `tools/publish-apt.sh`:
- the pool keeps the newest 3 versions of each package per suite, and older ones are pruned;
- `apt-ftparchive packages` indexes every version in the pool, so all 3 stay installable;
- `apt-ftparchive release` writes `Release` with Origin, Label, Suite, Codename and `Valid-Until`;
- `gpg` produces `InRelease` and `Release.gpg`.

`aptly` 1.6.1 is also in Trixie and keeps several versions natively. Its publish command has no `Valid-Until` option, though, which the weekly re-signing relies on. If you'd rather keep `reprepro` and accept "latest version only" for rollbacks, say so before deliverable 10.

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
| 5 | Debian packaging (7 packages) | `packages/*/debian/**`; payloads for both keyrings, release (os-release, sources, pinning with `apt-cache policy` proof, unattended-upgrades drop-in, `nimbus-update`), branding (all themes and art, CC-BY-SA-4.0 text from SPDX) and the desktop metapackage |
| 6 | Live hooks | `build/config/hooks/live/*.hook.chroot` |
| 7 | Live-only files and boot menus | `build/config/includes.chroot_after_packages/**`, `build/config/bootloaders/**` |
| 8 | Nimbus Control Center | `control-center/**` (backend, common, privd, frontend, bin, systemd, data, tests), in several batches |
| 9 | Catalog | `catalog/SCHEMA.md`, `catalog/manifest.schema.json`, 10 apps |
| 10 | CI/CD and docs (CI also runs `tools/tests/*.test.sh` and `tools/fetch-docker-key.sh --verify-local` / `--check`) | `.github/workflows/*`, `.github/dependabot.yml`, `apt/conf/*`, `tools/{publish-apt.sh,releases-json.py,split-iso.sh,pin-actions.sh}`, `control-center/tests/integration/samba-ucf-keeps-include.sh`, `README.md`, `LICENSE`, `LICENSES/*`, `.shellcheckrc`, `ruff.toml`, `docs/{RELEASING,SIGNING}.md`, `docs/release-notes/v0.1.0.md` |
| 11 | Testing | `docs/TESTING.md`, `tools/qemu-test.sh` |

---

## 9. Decisions from review (2026-11-17)

1. **Maintainer.** Your name and address as set in `NIMBUS_MAINTAINER` (`build/nimbus.conf`), the only place it is written. It reaches `debian/control` and the changelog trailers from there, and stays out of commit trailers, code comments, docs and test data (A11). This replaced the earlier no-reply placeholder; the placeholder warning and the planned release-workflow block were removed.
2. **License.** GPL-3.0-or-later for code and packaging, CC-BY-SA-4.0 for artwork, DEP-5 `debian/copyright`, fonts left in Debian's packages (A12, NOTE-13). The CC-BY-SA-4.0 text comes from SPDX's `license-list-data` (`text/CC-BY-SA-4.0.txt`), pinned to a commit that is noted in `debian/copyright` (deliverable 5).
3. **Network.** Allowed in deliverables 4 and 10. In deliverable 4, Docker's key is committed only after its fingerprint matches, the fingerprint is written into a comment, and CI re-checks it. In deliverable 10, actions are pinned to released-tag SHAs with a tag comment, and Dependabot is set up. Both get re-runnable fetch-and-verify scripts. `verify-trixie.sh` was also run against deb.debian.org in deliverable 3 (§7).
4. **Review changes.** NOTE-03 (no hand-run stages; one Docker source), NOTE-04 (repository check in `build.sh`), NOTE-14 (code expires after first use or 30 s), NOTE-23 (ucf CI test), NOTE-26 (hide "Pooled + parity" without snapraid), NOTE-34 (locks, random delay, never reboot), NOTE-35 (journal), NOTE-41 (xz, sums for parts and ISO, PowerShell join), NOTE-43 (ruff on the runner only).
5. **Docker keyring package.** Docker's key ships in its own package, `nimbusos-docker-keyring`, which `nimbusos-desktop` depends on. That makes seven packages. `build.sh` checks the finished image's APT setup (§2, step 6).
6. **Test environment.** `tools/dev-shell.sh` runs a `debian:trixie` container with Docker or Podman. The version-mapping tests pass there with real `dpkg` 1.22.22: 40 of 40 checks.
