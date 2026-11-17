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
├── LICENSE                           GPL-3.0-or-later (assumption A12)
├── .gitattributes                    LF line endings everywhere (NOTE-50)
├── .gitignore                        build products, caches, anything that looks like a private key
├── .shellcheckrc                     repo-wide shellcheck settings
├── ruff.toml                         ruff settings for all Python in the repo
│
├── .github/workflows/
│   ├── ci.yml                        push + PR: shellcheck, ruff, pytest, verify-trixie, build all .debs
│   ├── release.yml                   tags vX.Y.Z / vX.Y.Z-beta.N: debs → APT → ISO → GitHub Release
│   └── refresh.yml                   weekly: re-sign Release files; bootstraps an empty repo once (NOTE-04)
│
├── build/
│   ├── nimbus.conf                   every build tunable, one place (names, URLs, mirrors, live user, split size…)
│   ├── build.sh                      host checks → .debs → lb clean/config → lb stages → ISO + .sha256
│   ├── lib/common.sh                 logging, nimbus.conf loading, version helpers (shared with tools/)
│   └── config/                       live-build config TEMPLATE; copied to build/work/ for each build
│       ├── package-lists/
│       │   ├── 10-base.list.chroot
│       │   ├── 20-plasma.list.chroot
│       │   ├── 30-theming-fonts.list.chroot
│       │   ├── 40-calamares.list.chroot             marked "live-only": not mirrored into nimbusos-desktop
│       │   ├── 50-docker.list.chroot
│       │   ├── 60-virtualization.list.chroot
│       │   ├── 70-storage-nas.list.chroot
│       │   ├── 90-nimbusos.list.chroot              nimbusos-desktop itself (marked "live-only")
│       │   └── 95-installer-pool.list.binary        GRUB/shim packages placed in the ISO pool for Calamares
│       ├── archives/
│       │   ├── docker.list.chroot                   Docker CE source for the build (NOTE-03)
│       │   └── docker-archive-keyring.asc           Docker's public key; fingerprint pinned in nimbus.conf
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
│       └── bootloaders/                  overlays merged onto live-build's own templates at build time
│           ├── grub-pc/                  GRUB (EFI) live menu: labels + theme.cfg; the theme comes from nimbusos-branding
│           └── isolinux/                 ISOLINUX menu: stdmenu.cfg, labels, splash.svg (rendered to PNG at build)
│
├── packages/                         one Debian source package per directory: debhelper-compat (= 13), 3.0 (native)
│   ├── nimbusos-archive-keyring/     keys/*.asc → dearmored keyrings
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
│
├── catalog/
│   ├── SCHEMA.md                     manifest schema, documented field by field
│   ├── manifest.schema.json          the same schema as JSON Schema (checked in CI and at runtime)
│   └── <app-id>/{manifest.json, docker-compose.yml.j2, icon.svg}
│        portainer, uptime-kuma, vaultwarden, nextcloud-aio, jellyfin,
│        pihole, syncthing, home-assistant, grafana, nginx-proxy-manager
│
├── apt/conf/{distributions,options}  reprepro config: stable + beta, Limit 3, ValidFor 14d
│
├── tools/
│   ├── build-debs.sh                 stage + build all 6 .debs (used by build.sh, ci.yml, release.yml)
│   ├── deb-version.sh                git tag → Debian version (NOTE-32)
│   ├── gen-desktop-depends.sh        package lists → ${nimbus:ListDepends} substvar (NOTE-48)
│   ├── verify-trixie.sh              proves every listed package and live-build flag exists in trixie (§7)
│   ├── publish-apt.sh                reprepro include/export + releases.json + signing (release + refresh)
│   ├── releases-json.py              maintains apt/releases.json
│   ├── split-iso.sh                  splits ISOs over the GitHub asset limit; writes join instructions
│   └── qemu-test.sh                  the UEFI/BIOS QEMU test commands from docs/TESTING.md
│
└── docs/
    ├── PLAN.md                       this file
    ├── RELEASING.md                  one-time GitHub setup, stable/beta releases, rollback
    ├── SIGNING.md                    offline ed25519 key, secrets, key rotation
    ├── TESTING.md                    QEMU commands + smoke-test checklist
    └── release-notes/v0.1.0.md       release notes = GitHub Release body = releases.json notes (NOTE-37)
```

Rules for the whole tree:

- **`build/config/` is a template.** `build.sh` copies it to `build/work/config/`, adds the generated pieces there (the built `.deb` files, merged bootloader templates, rendered splash images), and runs live-build inside `build/work/`. A build never modifies the checked-in tree.
- **Anything that may change after install ships in a package.** `includes.chroot_after_packages/` holds only files used by the live session (Calamares configuration, live-only units and launchers). Calamares' last step deletes those files from the installed target.
- **No binary files are committed.** PNGs, GRUB `.pf2` fonts and binary keyrings are generated when the packages are built (NOTE-12).

---

## 2. Build and release flow

```
git tag -a v0.2.0  ──►  release.yml   (concurrency group "nimbusos-publish")
   │
   ├─ build-debs      container debian:trixie      tools/build-debs.sh → 6 × .deb ──► artifact "debs"
   ├─ publish-apt     container debian:trixie      tools/publish-apt.sh → gh-pages:/apt/{dists,pool}
   │                                               suite stable (or beta), signed, releases.json(.sig)
   ├─ build-iso       privileged debian:trixie     build/build.sh --debs-dir debs/ ──► artifact "iso"
   └─ github-release  ubuntu runner, gh CLI        SHA256SUMS + SHA256SUMS.gpg, split > 2 GiB, publish
```

**`build/build.sh`** (local use: `sudo ./build/build.sh`; or `--from-repo [--version X]`):

1. Checks that it runs as root on Debian 13 with enough free disk space. Installs `live-build`, `debootstrap`, `devscripts` and the package build dependencies if any are missing.
2. Gets the `.deb` files, either by building them with `tools/build-debs.sh` or, with `--from-repo`, by downloading them with `apt-get download` through a temporary APT configuration that uses `Signed-By` with the NimbusOS key. They go into `build/work/config/packages.chroot/`.
3. Prepares `build/work/`: copies the config, merges the bootloader overlays onto `/usr/share/live/build/bootloaders/*`, and extracts the GRUB theme from the freshly built `nimbusos-branding` package, so the live boot and installed boot use the same theme.
4. Runs `lb clean`, then `lb config`. Every flag is explained in the script.
5. Runs `lb bootstrap`, copies the Docker keyring into the chroot (NOTE-03), runs `lb chroot`, checks the APT files in the image, then runs `lb installer` and `lb binary`.
6. Renames the output to `nimbusos-amd64.iso` and writes `nimbusos-amd64.iso.sha256`.

**Version mapping** (one version for all six packages, taken from git):

| Git ref | Debian version | APT suite | `NIMBUSOS_CHANNEL` in os-release |
|---|---|---|---|
| `v0.2.0` | `0.2.0` | `stable` | `stable` |
| `v0.2.1-beta.3` | `0.2.1~beta.3` | `beta` | `beta` |
| 5 commits after `v0.2.0`, untagged | `0.2.0+git20261117.5.g1a2b3c4` | not published | `dev` |

`debian/changelog` in each package holds the history. `tools/build-debs.sh` adds the top entry for the computed version at build time, using `dch` from devscripts.

---

## 3. Packages

| Package | Ships | Key relationships | Maintainer-script duties |
|---|---|---|---|
| `nimbusos-archive-keyring` | `/usr/share/keyrings/nimbusos-archive-keyring.gpg` (every `keys/nimbusos-*.asc`, so it can hold old + new keys during rotation); `/usr/share/keyrings/nimbusos-docker-archive-keyring.gpg` (NOTE-05) | none | none |
| `nimbusos-release` | `/usr/lib/os-release` (diverted, NOTE-36); `/etc/apt/sources.list.d/nimbusos.sources` + `nimbusos-docker.sources` (deb822, `Signed-By`); `/etc/apt/preferences.d/nimbusos.pref` + `nimbusos-docker.pref`; `/etc/apt/apt.conf.d/21nimbusos-periodic` + `52nimbusos-unattended-upgrades`; `/usr/bin/nimbus-update` + man page; `nimbus-update-check.timer`, `nimbus-update-auto.timer`, `nimbus-flathub-setup.service` | Depends: `nimbusos-archive-keyring (>= ver)`, `python3`, `python3-apt`, `unattended-upgrades`, `gpgv` | preinst: `dpkg-divert --rename` of base-files' os-release. postinst: create `/etc/nimbusos/update.conf` if missing; recreate the beta source if Beta is selected. postrm: remove the diversion; on purge, remove the generated files |
| `nimbusos-branding` | GRUB theme + `/etc/default/grub.d/10-nimbusos.cfg`, Plymouth theme, SDDM theme + `/etc/sddm.conf.d/10-nimbusos.conf`, Calamares branding, wallpaper, Plasma global theme `org.nimbusos.desktop` (with `layout.js`), `NimbusDark.colors`, `/etc/xdg` defaults, logo icons | Depends: `papirus-icon-theme`, `fonts-inter`, `fonts-jetbrains-mono`, `plymouth`, `plymouth-label` | postinst: register `default.plymouth` alternative; `update-grub` if GRUB is installed; initramfs trigger |
| `nimbus-control` | backend, frontend, `nimbus-privd`, units, Samba drop-in, polkit action, launcher | Depends: the Python stack (`python3-fastapi`, `python3-uvicorn`, `python3-websockets`, `python3-docker`, `python3-libvirt`, `python3-psutil`, `python3-jinja2`, `python3-yaml`, `python3-jsonschema`, `python3-systemd`), `docker-ce`, `docker-compose-plugin`, `libvirt-daemon-system`, `samba`, `wsdd`, `avahi-daemon`, `novnc`, `websockify`, `chromium`, … | Creates the groups `nimbus-control` and `nimbus-smb`, the system users `nimbus-control` and `nimbus-guest` (NOTE-25), and adds `nimbus-control` to `docker`, `libvirt` and `nimbus-smb`. Runs schema migrations step by step. Adds the smb.conf include (NOTE-23) and the `adduser.conf` keys (NOTE-09). Enables services on first install only. Restarts its own services only when their files changed |
| `nimbus-catalog` | `/usr/share/nimbus-control/catalog/<app-id>/` | Enhances: `nimbus-control` | none. The backend notices catalog changes on its own and marks installed stacks with "Template update available" |
| `nimbusos-desktop` | nothing but docs | Depends: `${nimbus:ListDepends}` (generated) + the five packages above at `(>= ver)` | none |

Rules applied to all six: lintian-clean or a justified override; idempotent, upgrade-safe maintainer scripts; user-editable files are conffiles or `.d` drop-ins; services restarted with `deb-systemd-invoke` only when their files changed (`dh_installsystemd --no-restart-after-upgrade` plus a checksum comparison in postinst).

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
| `smbd`, `wsdd`, `avahi-daemon` | Debian defaults | 445, 3702/5357, 5353 | NAS; `nmbd` off by default |

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
- `install`: run by privd as a transient unit (`systemd-run --unit=nimbus-update-<id>`) so it survives restarts of the services it upgrades (NOTE-35). `apt-get update` then `apt-get full-upgrade` with `DEBIAN_FRONTEND=noninteractive`, `--force-confdef` and `--force-confold`, `UCF_FORCE_CONFFOLD=1`, and `-o DPkg::Lock::Timeout=…` so it waits for unattended-upgrades instead of colliding with it. The log is written to `/var/log/nimbusos/` and streamed to the UI drawer through privd.
- Channels: Stable is the packaged `nimbusos.sources`. Beta adds a generated `nimbusos-beta.sources` (same `Signed-By`). See NOTE-33.
- Policy: unattended-upgrades handles Debian-Security only. NimbusOS updates follow `POLICY` through nimbus-update's own timers (NOTE-34). The system never reboots on its own.

---

## 5. Assumptions

| # | Assumption |
|---|---|
| A1 | The GitHub repository stays public, Pages is served from the `gh-pages` branch root, and the URLs in the header table are final. Both URLs are in `build/nimbus.conf`. |
| A2 | Builds run on a Debian 13 host or a privileged `debian:trixie` container with network access and at least 40 GB free. GitHub runners need their preinstalled toolchains removed first to make room (done in `release.yml`). |
| A3 | Docker publishes `docker-ce`, `docker-ce-cli`, `containerd.io` and `docker-compose-plugin` for `https://download.docker.com/linux/debian trixie stable`, signed by the key with fingerprint `9DC8 5822 9FC7 DD38 854A E2D8 8D81 803C 0EBF CD88`. |
| A4 | Trixie ships `live-build` with the `config/includes.chroot_after_packages/` layout and the `grub-pc` / `isolinux` bootloader template directories, and Calamares 3.3 with `calamares-settings-debian` providing the `bootloader-config` and `sources-media` steps. |
| A5 | Trixie's `reprepro` supports `Limit:` (keep several versions per package) and `ValidFor:` (adds `Valid-Until`). |
| A6 | Debian's signed GRUB finds the `grub.cfg` that sits next to it whatever the EFI directory is called (checked by smoke test 1; fallback in NOTE-07). |
| A7 | The UI is English-only; the live session uses `en_US.UTF-8` and a `us` keyboard (both tunable in `nimbus.conf`); Calamares lets the user change both. |
| A8 | Minimum hardware: x86-64-v1 CPU with VT-x/AMD-V for the VM tab, 4 GB RAM, a 40 GB system disk. |
| A9 | Default hostname `nimbusos`; connection strings and dashboard links use the machine's actual hostname, so a renamed machine still gets correct links. |
| A10 | The maintainer generates the archive signing key offline as described in `docs/SIGNING.md` and commits only the public key (NOTE-44). |
| A11 | The Maintainer name and e-mail in every package come from `NIMBUS_MAINTAINER` in `nimbus.conf` (see §9). |
| A12 | License is GPL-3.0-or-later for all original code and art (see §9). |

---

## 6. NOTE: deviations, limitations and interpretations

### Build and image

**NOTE-01 — Rounded window corners.** In Trixie's Plasma, the Breeze window decoration rounds only the title-bar (top) corners. KWin has no built-in option to round all corners, and the third-party effects that do are not in Debian. NimbusOS therefore keeps Breeze's rounding and adds nothing more.

**NOTE-02 — `includes.chroot` folder name.** Current live-build reads live-only files from `config/includes.chroot_after_packages/` (copied after package installation, overriding packaged files inside the live image). NimbusOS uses that name instead of the older `config/includes.chroot/`.

**NOTE-03 — `lb build` runs as separate stages.** apt refuses to work when two entries for the same repository carry different `Signed-By` values ("Conflicting values set for option Signed-By"). Installed systems get the Docker source from `nimbusos-release`, signed by `/usr/share/keyrings/nimbusos-docker-archive-keyring.gpg`, and that package is unpacked midway through the build. The build-time `config/archives/docker.list.chroot` therefore uses the same `signed-by` path. For that path to exist before `lb chroot` first runs `apt-get update`, `build.sh` copies the key into the chroot right after `lb bootstrap`. So `build.sh` calls `lb bootstrap`, `lb chroot`, `lb installer` and `lb binary` itself (the stages `lb build` runs) instead of one `lb build`. Between `lb chroot` and `lb binary` it also checks that the packaged APT files are present in the image and match the `.deb`.

**NOTE-04 — The first ISO build needs a published APT repository.** When live-build switches the image to its final APT sources at the end of `lb chroot`, it refreshes the indices. `nimbusos-release` is installed by then, so the NimbusOS URL has to serve a signed repository; an empty one is enough. `release.yml` publishes to APT before it builds the ISO. For the very first setup, `refresh.yml` creates an empty signed repository when `gh-pages` doesn't exist yet (`docs/RELEASING.md`, one-time setup).

**NOTE-05 — The Docker source is a NimbusOS package too.** On installed systems the Docker CE source (`nimbusos-docker.sources`) ships in `nimbusos-release`, and Docker's key ships in `nimbusos-archive-keyring`. A Docker key rotation then arrives as a normal NimbusOS update. The Docker origin is pinned the same way as NimbusOS's: priority 1 for everything, 500 for `docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin`, `docker-buildx-plugin` and `docker-ce-rootless-extras`. Docker's repository therefore cannot replace Debian packages.

**NOTE-06 — Calamares configuration is live-only.** Calamares runs only in the live session, and its own `packages` step purges it from the installed system. `settings.conf` and the module overrides therefore live in `includes.chroot_after_packages/`, where they replace `calamares-settings-debian`'s copies inside the live image. The branding component `/usr/share/calamares/branding/nimbusos/` ships in `nimbusos-branding`, as specified. Debian's `bootloader-config` and `sources-media` steps are kept. Debian's `sources-final` is replaced by `nimbus-sources-final`, which writes deb822 `/etc/apt/sources.list.d/debian.sources` with `main contrib non-free non-free-firmware` for `trixie`, `trixie-updates` and `trixie-security`, and empties the legacy `sources.list`. The NimbusOS and Docker sources are already on the target, because `nimbusos-release` is part of the copied image. A final `nimbus-target-cleanup` step deletes the live-only files from the target. `users.conf` uses the Calamares 3.3 `hostname:` block with `template: "nimbusos"`.

**NOTE-07 — Secure Boot and the EFI directory.** Installed systems boot through Debian's signed shim and GRUB, which NimbusOS does not rebuild, so Secure Boot keeps working. `/etc/default/grub.d/10-nimbusos.cfg` sets `GRUB_DISTRIBUTOR=NimbusOS`. That makes the boot entries read "NimbusOS" and makes `grub-install` use `EFI/nimbusos/`. Calamares' `efiBootloaderId` is set to the same value, so the installer and later GRUB package upgrades agree, and `installEFIFallback: true` also writes `EFI/BOOT/BOOTX64.EFI`. This depends on assumption A6. If smoke test 1 fails with Secure Boot on, the fallback is `GRUB_DISTRIBUTOR=Debian`: the EFI directory stays `EFI/debian`, but boot entries then read "Debian GNU/Linux".

**NOTE-08 — The SDDM greeter stays on X11.** SDDM's Wayland greeter is less dependable in VMs and on some GPUs, so the login screen uses Debian's default X11 greeter with the NimbusOS QML theme. The **session** defaults to Plasma (Wayland): the live autologin uses `Session=plasma`, and new installs get SDDM's last-session state pre-set to `plasma.desktop`. Plasma (X11) remains selectable.

**NOTE-09 — `/etc/adduser.conf`.** The file belongs to the `adduser` package and has no drop-in directory. The `nimbus-control` postinst therefore sets exactly two keys in place, `EXTRA_GROUPS="docker libvirt kvm nimbus-control"` and `ADD_EXTRA_GROUPS=1`. It does this idempotently and only when they differ. This is a deliberate, documented exception to Debian Policy §10.7.4. `EXTRA_GROUPS` only applies to accounts made with `adduser`. Plasma's Users settings page creates accounts through AccountsService and `useradd`, which ignore it. For such users the launcher detects the missing group and offers **Grant access**, which asks for an administrator password through polkit; the user then logs out and back in.

**NOTE-10 — How Plasma theme updates reach existing users.** The global theme, panel layout and wallpaper are applied at a user's first login. After that, Plasma stores them per user, and NimbusOS does not overwrite them. Colors, accent, icons, fonts, widget style and KWin effects come from `/etc/xdg` defaults, so existing users do get updates there unless they changed those settings themselves.

**NOTE-11 — Flathub.** The `.flatpakrepo` file carries Flathub's signing key. It is downloaded over HTTPS, which is Flathub's documented method. Writing that key into this repo from memory would be inventing it. `nimbus-flathub-setup.service` (in `nimbusos-release`) therefore runs `flatpak remote-add --if-not-exists --system flathub https://dl.flathub.org/repo/flathub.flatpakrepo` once after the network is up, and the build hook `0100` runs the same packaged script, so the ISO already contains Flathub. Systems that get NimbusOS through an update receive it at their next boot with a network connection.

**NOTE-12 — No binary assets.** All artwork is SVG. GRUB, Plymouth, ISOLINUX, SDDM and Calamares need PNG files, so `nimbusos-branding` renders them during the package build (`rsvg-convert` from `librsvg2-bin`), and GRUB fonts are generated with `grub-mkfont` from `fonts-inter`. The Plasma wallpaper package ships the SVG directly.

**NOTE-13 — Web UI fonts come from Debian packages.** The UI uses Inter and JetBrains Mono from `fonts-inter` and `fonts-jetbrains-mono`, served by the backend at `/assets/fonts/*` (it finds the files through fontconfig at startup), with `local()` first in each `@font-face`. The fonts are still local and work offline with no CDN, but no font files are copied into the repo.

### Control Center

**NOTE-14 — The launcher passes a one-time launch code, not the token.** A token on the Chromium command line would be readable by every local user through `/proc/<pid>/cmdline`. `nimbus-control-open` reads `/run/nimbus-control/token`, trades it with the backend for a single-use code valid for 30 seconds, and runs `chromium --app=http://127.0.0.1:7777/?launch=<code> --class=NimbusControl --user-data-dir=<per-user profile>`. The page trades the code for the session token, keeps the token in `sessionStorage`, and removes the code from the address bar. If the user is not in `nimbus-control`, the launcher explains this and offers **Grant access** (NOTE-09).

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

**NOTE-23 — A single include line in `smb.conf`.** Debian's `smb.conf` has no include directory, and Samba reads nothing else. The `nimbus-control` postinst appends one marked block to the end of `/etc/samba/smb.conf`: `include = /etc/samba/nimbus/nimbus.conf`. Everything else NimbusOS sets lives in `/etc/samba/nimbus/` (static globals as a conffile; generated `settings.conf`) and in `/etc/samba/nimbus-shares.conf`. Debian's ucf handling of `smb.conf` otherwise stays as it is. The updater sets `UCF_FORCE_CONFFOLD=1` so a Samba upgrade doesn't replace the file. privd checks for the include before each `share.apply` and on startup, puts it back if it is missing, and raises an alert when it does.

**NOTE-24 — Samba's own mDNS registration is turned off.** `multicast dns register = no` stops smbd from advertising itself through Avahi. The generated `/etc/avahi/services/smb.service` then becomes the only advertisement, which avoids duplicate entries in Finder and lets NimbusOS add the `_adisk` record for Time Machine.

**NOTE-25 — A dedicated guest account.** Public shares should be writable by guests without world-writable folders. The package therefore creates a `nimbus-guest` system user (nologin, no home, member of `nimbus-smb`) and sets it as Samba's `guest account`. `map to guest = Bad User` is set only while at least one Public share exists.

**NOTE-26 — SnapRAID.** `/etc/snapraid.conf` may belong to Debian's snapraid package, so NimbusOS writes `/etc/nimbus/snapraid.conf` and always calls `snapraid -c`. Parity is **scheduled, not real time**: nightly sync, and a weekly scrub of 8 % of the array covering blocks older than 10 days. The pool card and the wizard both say so. privd refreshes SMART data hourly with `smartctl -n standby`, so spun-down disks are not woken. Availability: see §7. If `snapraid` turns out to be missing from Trixie, the fallback is to build it from the upstream source as an extra package in the NimbusOS APT repository, with priority 500.

**NOTE-27 — The ISO library when an `isos` share exists.** The VM tab always uses `/var/lib/nimbus-vms/isos`. When an `isos` share exists, privd bind-mounts the share's folder onto that path, through a managed fstab line. `nimbus-control` then keeps one narrow `ReadWritePaths=` entry, and ISOs dropped in over SMB show up in the VM wizard.

### Catalog

**NOTE-28 — Catalog icons.** Each app gets a neutral monogram SVG (initials on the app's category color), not the vendor's logo. This avoids trademark problems and binary files.

**NOTE-29 — Nextcloud AIO and Vaultwarden are exceptions.** Nextcloud AIO requires the container name `nextcloud-aio-mastercontainer`, the named volume `nextcloud_aio_mastercontainer` (its data is not under the stack directory) and the Docker socket. It also starts its own `nextcloud-aio-*` containers, which the dashboard groups under the AIO app. Vaultwarden's web vault needs a secure context, so its manifest sets `webui.secure_context: true` and its Open link uses `http://localhost:<port>` instead of the hostname. For LAN access, the notes point to Nginx Proxy Manager with TLS.

**NOTE-30 — Compose templates and the raw editor.** Every value inserted into a `.j2` template goes through a JSON-quoting filter (`| q`) in a Jinja2 `SandboxedEnvironment` with `StrictUndefined`. That rules out YAML injection. In the raw editor, a bind mount must resolve inside the stack directory, inside a share, or to one of the host paths listed in the app's manifest (for example `/var/run/docker.sock` for Portainer and AIO, `/run/dbus` for Home Assistant). Anything else is rejected with a message that says which path broke the rule.

**NOTE-31 — Stack data owned by container users.** Containers write their data as root or as their own UID (Grafana uses 472), which `nimbus-control` cannot change or delete. `stack.prepare_volume` (chown to the UID in the manifest) and `stack.delete_data` are privd verbs, limited to paths inside `/var/lib/nimbus-control/stacks/<stack>/`.

### Updates and releases

**NOTE-32 — Beta tags map to `~beta` versions.** A tag `vX.Y.Z-beta.N` becomes Debian version `X.Y.Z~beta.N`, so the final `X.Y.Z` sorts above all of its betas. (`X.Y.Z-beta.N` would be an invalid native version, and Debian would sort it above `X.Y.Z`.)

**NOTE-33 — Switching from Beta back to Stable doesn't downgrade.** Beta adds the `beta` suite on top of `stable`, both at priority 500, so apt picks the highest version. Switching back to Stable removes the `beta` suite, and the system keeps its beta versions until Stable passes them. The confirmation dialog says this, and `docs/RELEASING.md` shows an explicit downgrade with `apt install <pkg>=<version>`.

**NOTE-34 — unattended-upgrades installs security updates only.** Debian's default unattended-upgrades configuration also installs regular point-release updates. The NimbusOS drop-in (`52nimbusos-unattended-upgrades`) uses apt's `#clear Unattended-Upgrade::Origins-Pattern;` and then re-adds only the Debian-Security origins, and keeps `Automatic-Reboot "false"`. NimbusOS updates are **not** routed through unattended-upgrades, because it refuses to install new dependencies from origins outside its allowlist, and new Debian dependencies of `nimbusos-desktop` are how new software reaches existing installs. Instead, `nimbus-update-auto.timer` runs `nimbus-update auto` daily under the **Automatic** policy (`apt-get install --no-remove` of the installed `nimbus-*`/`nimbusos-*` packages, which pulls in new dependencies). **Notify only** runs just the daily check. **Off** runs neither, though Check now still works. Regular Debian updates arrive through **Install updates** or Discover.

**NOTE-35 — Updates run in a transient unit.** Upgrading `nimbus-control` restarts `nimbus-privd`. If apt were a child of privd, that restart would kill dpkg midway. privd therefore starts `nimbus-update install` with `systemd-run` as its own unit and follows the log file. If the page reloads during an update, the drawer picks the log back up.

**NOTE-36 — os-release.** `base-files` owns `/usr/lib/os-release`, so `nimbusos-release` diverts it (`dpkg-divert --rename`) and ships its own: `NAME="NimbusOS"`, `ID=nimbusos`, `ID_LIKE=debian`, `VERSION_ID`, `VERSION`, and `NIMBUSOS_CHANNEL`, a vendor-prefixed custom field, which os-release(5) allows. It keeps `VERSION_CODENAME=trixie`, because unattended-upgrades, Docker and other tools look up the release by codename. `/etc/os-release` stays base-files' symlink and therefore points to the NimbusOS file.

**NOTE-37 — Where release notes come from.** The spec takes release notes from the GitHub Release body, but the release is created in the last step, after `releases.json` has been published. The single source is therefore `docs/release-notes/<tag>.md`, written before tagging. `release.yml` uses that file both as the GitHub Release body and as the notes in `releases.json`, and fails with a clear message if it is missing. A line `Restart-Required: yes` in the file sets `restart_required`.

**NOTE-38 — `releases.json` is signed.** `releases.json` gets a detached signature, `releases.json.sig`, made with the archive key, and `nimbus-update` verifies it with `gpgv` against the NimbusOS keyring before showing any release notes. The notes are therefore as trustworthy as the packages.

**NOTE-39 — Pages is published from a branch.** GitHub Pages serves the `gh-pages` branch, so the workflows need only `contents: write`. `pages: write` and `id-token: write` belong to the artifact-based `deploy-pages` method, which isn't used. Each publish force-pushes `gh-pages` as a single fresh commit, which keeps the repository from growing with every release. reprepro's `db/` is kept in the branch: it is public metadata and contains no secrets.

**NOTE-40 — Scheduled workflows are paused after 60 days of inactivity.** GitHub disables scheduled workflows in public repositories that see no activity for 60 days. If `refresh.yml` stops, `InRelease` expires 14 days after its last signing, and installed systems show the "expired metadata" error, which explains how to fix it. `docs/RELEASING.md` explains how to re-enable the workflow.

**NOTE-41 — ISOs over 2 GiB are split.** A Plasma desktop with Docker, QEMU/libvirt, Chromium, Firefox and firmware is expected to be roughly 3.5–4.5 GB, above GitHub's 2 GiB limit per release asset. `tools/split-iso.sh` splits the ISO into 1,900 MiB parts (`nimbusos-amd64.iso.part00`, `.part01`, …). `SHA256SUMS` lists the full ISO and every part, and the release body and README give join commands for Linux/macOS (`cat nimbusos-amd64.iso.part* > nimbusos-amd64.iso`) and Windows (`copy /b nimbusos-amd64.iso.part00 + nimbusos-amd64.iso.part01 nimbusos-amd64.iso`). An ISO under the limit is uploaded whole.

### CI and repository

**NOTE-42 — Actions are pinned to commit SHAs.** Workflows use only GitHub's own actions (`actions/checkout`, `actions/upload-artifact`, `actions/download-artifact`), each pinned to a full commit SHA with the tag in a comment. Releases are created with the preinstalled `gh` CLI. No marketplace actions are used. The SHAs are looked up with `gh api` when the workflows are written (deliverable 10), not from memory.

**NOTE-43 — ruff in CI.** If Trixie packages `ruff`, CI installs it with apt. Otherwise CI installs a pinned ruff into a throwaway virtualenv inside the CI container. That is the only `pip` use in the project, it never touches the OS image, and the lint job's log says which path was taken.

**NOTE-44 — Bootstrapping the signing key.** The repository can only hold the public key after you have generated it (`docs/SIGNING.md`). Until `packages/nimbusos-archive-keyring/keys/` contains a key, `ci.yml` builds the packages with a throwaway key generated inside the job and prints a warning. `release.yml` refuses to run without a committed public key and checks that its fingerprint matches the `APT_SIGNING_KEY` secret.

**NOTE-45 — Desktop-user groups and the `docker` group.** As specified, desktop users (live, Calamares and `adduser`) join `docker`, `libvirt`, `kvm` and `nimbus-control`. Membership in `docker` or `nimbus-control` is effectively root. `README.md` says this plainly, and the Control Center is meant for the machine's administrator.

**NOTE-46 — README location.** `README.md` sits at the repository root, which is what GitHub shows on the project page. `docs/` holds `RELEASING.md`, `SIGNING.md` and `TESTING.md`.

**NOTE-47 — Package build layout.** `packages/nimbus-control` and `packages/nimbus-catalog` contain only `debian/`. `tools/build-debs.sh` stages each package into `_build/src/<pkg>-<version>/`, copying `control-center/` or `catalog/` alongside, and builds there. The source trees stay where the spec puts them, and each package still builds from a self-contained directory.

**NOTE-48 — Which lists feed `nimbusos-desktop`.** `nimbusos-desktop` gets its Depends from the package lists at build time, through a substvar, so the lists and the metapackage can't drift apart. Lists whose header contains `#! nimbus: live-only` are skipped: Calamares, which removes itself after install (if the metapackage depended on it, removing Calamares would remove the metapackage), and `90-nimbusos` (the metapackage itself). GRUB packages are kept out too, because the installer chooses between `grub-pc` and `grub-efi-amd64`.

**NOTE-49 — `nimbus.conf` values reach the packages.** Build tunables that installed systems need (the APT URL, the `releases.json` URL, the Maintainer) are substituted from `build/nimbus.conf` into package templates at build time, so `nimbus.conf` remains the one place to change them.

**NOTE-50 — Developing on Windows.** The repository is edited on Windows with `core.filemode=false`. `.gitattributes` forces LF line endings, and executable bits are recorded explicitly with `git update-index --chmod=+x` for every script, hook and `debian/rules`.

---

## 7. Verification status

The online check of package names and flags against the Trixie archive was declined in this session, so nothing in this plan has yet been confirmed against the live archive. The project checks this mechanically instead, so CI fails on any invented name:

- `tools/verify-trixie.sh` runs in `ci.yml` inside `debian:trixie` with `main contrib non-free non-free-firmware` and the Docker source enabled.
  1. `apt-get install --simulate` of every package in every list, plus every `Depends:` of the six packages, fails on any package that doesn't exist.
  2. `lb config` with `build.sh`'s exact flag set in a scratch directory fails on any option the Trixie live-build doesn't know.
  3. `testparm` and `snapraid`/`mergerfs` option checks run against the generated sample configs from the test suite.

| Confidence | Names |
|---|---|
| Expected present | `live-build`, `debootstrap`, `calamares`, `calamares-settings-debian`, `kde-plasma-desktop`, `sddm`, `plymouth`, `firefox-esr`, `chromium`, `konsole`, `dolphin`, `plasma-discover`, `flatpak`, `pipewire`, `pipewire-pulse`, `wireplumber`, `network-manager`, `papirus-icon-theme`, `fonts-jetbrains-mono`, `samba`, `smbclient`, `avahi-daemon`, `mergerfs`, `smartmontools`, `parted`, `btrfs-progs`, `xfsprogs`, `e2fsprogs`, `exfatprogs`, `ntfs-3g`, `qemu-system-x86`, `qemu-utils`, `libvirt-daemon-system`, `libvirt-clients`, `virtinst`, `ovmf`, `bridge-utils`, `swtpm`, `swtpm-tools`, `novnc`, `websockify`, `python3-websockify`, `python3-fastapi`, `python3-uvicorn`, `python3-docker`, `python3-libvirt`, `python3-psutil`, `python3-jinja2`, `python3-yaml`, `python3-apt`, `python3-systemd`, `unattended-upgrades`, `reprepro`, `intel-microcode`, `amd64-microcode`, `firmware-linux`, `firmware-misc-nonfree`, `firmware-amd-graphics`, `firmware-iwlwifi`, `firmware-realtek` |
| Must be confirmed by `verify-trixie.sh` | `snapraid` (fallback in NOTE-26), `wsdd`, `samba-vfs-modules` (Trixie's split of VFS modules between `samba` and this package), `fonts-inter` (vs. `fonts-inter-variable`), `plasma-discover-backend-flatpak`, the PackageKit backend (Debian builds it into `plasma-discover`), `kwin-wayland`/`kwin-x11`, `libvirt-daemon-config-network`, `python3-websockets`, `plymouth-label`, `firmware-sof-signed`, `ruff` (NOTE-43) |
| Fetched, never typed | Docker's ASCII-armored key (fingerprint checked against A3) and the action commit SHAs (NOTE-42) |

---

## 8. Deliverable map

| # | Deliverable | Files |
|---|---|---|
| 1 | Plan | `docs/PLAN.md`, `.gitattributes`, `.gitignore` |
| 2 | Build entry point | `build/nimbus.conf`, `build/build.sh`, `build/lib/common.sh`, `tools/build-debs.sh`, `tools/deb-version.sh` |
| 3 | Package lists | `build/config/package-lists/*`, `tools/gen-desktop-depends.sh`, `tools/verify-trixie.sh` |
| 4 | Build-time archives | `build/config/archives/docker.list.chroot`, `docker-archive-keyring.asc` |
| 5 | Debian packaging (6 packages) | `packages/*/debian/**`; payloads for keyring, release (os-release, sources, pinning with `apt-cache policy` proof, unattended-upgrades drop-in, `nimbus-update`), branding (all themes and art) and the desktop metapackage |
| 6 | Live hooks | `build/config/hooks/live/*.hook.chroot` |
| 7 | Live-only files and boot menus | `build/config/includes.chroot_after_packages/**`, `build/config/bootloaders/**` |
| 8 | Nimbus Control Center | `control-center/**` (backend, common, privd, frontend, bin, systemd, data, tests), in several batches |
| 9 | Catalog | `catalog/SCHEMA.md`, `catalog/manifest.schema.json`, 10 apps |
| 10 | CI/CD and docs | `.github/workflows/*`, `apt/conf/*`, `tools/{publish-apt.sh,releases-json.py,split-iso.sh}`, `README.md`, `LICENSE`, `.shellcheckrc`, `ruff.toml`, `docs/{RELEASING,SIGNING}.md`, `docs/release-notes/v0.1.0.md` |
| 11 | Testing | `docs/TESTING.md`, `tools/qemu-test.sh` |

---

## 9. Open questions (defaults are used if unanswered)

1. **Maintainer identity.** Which name and e-mail should appear as `Maintainer:` in every package and changelog? They are public in the `.deb` files and the APT repository. There is no default. Deliverable 5 needs an answer, and it will be stored as `NIMBUS_MAINTAINER` in `build/nimbus.conf`.
2. **License.** The default is GPL-3.0-or-later for code and art.
3. **Network access while writing deliverables 4 and 10.** Fetching Docker's public key (and checking its fingerprint) and looking up the action SHAs with `gh api` both need network access. Without it, those files are produced by scripts that fetch and verify the values, and you run the scripts once before the first release.
