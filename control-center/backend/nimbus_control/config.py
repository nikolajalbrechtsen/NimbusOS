# =============================================================================
# nimbus_control/config.py: where things are
# =============================================================================
# What: the backend's settings with the packaged defaults. Each can be
#       overridden with an environment variable NIMBUS_CONTROL_<NAME>
#       (for example NIMBUS_CONTROL_PORT), which the systemd unit doesn't
#       set; tests and development use them.
# Why:  one place for every path (docs/PLAN.md §4.2).
# =============================================================================

import dataclasses
import os


@dataclasses.dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 7777
    state_dir: str = "/var/lib/nimbus-control"
    run_dir: str = "/run/nimbus-control"
    privd_socket: str = "/run/nimbus-privd.sock"
    www_dir: str = "/usr/lib/nimbus-control/www"
    os_release: str = "/etc/os-release"
    logo_path: str = "/usr/share/icons/hicolor/scalable/apps/nimbusos.svg"
    catalog_dir: str = "/usr/share/nimbus-control/catalog"
    docker_socket: str = "/var/run/docker.sock"
    libvirt_uri: str = "qemu:///system"
    isos_dir: str = "/var/lib/nimbus-vms/isos"
    images_dir: str = "/var/lib/nimbus-vms/images"
    vnc_tokens_dir: str = "/run/nimbus-control/vnc-tokens"
    novnc_dir: str = "/usr/share/novnc"
    websockify_port: int = 6080
    iso_max_bytes: int = 32 * 1024 ** 3

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        values = {}
        for field in dataclasses.fields(cls):
            raw = env.get(f"NIMBUS_CONTROL_{field.name.upper()}")
            if raw is not None:
                values[field.name] = int(raw) if field.type in (int, "int") else raw
        return cls(**values)
