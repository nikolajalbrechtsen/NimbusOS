# =============================================================================
# nimbus_control/catalog.py: the app catalog (package nimbus-catalog)
# =============================================================================
# What: reads /usr/share/nimbus-control/catalog/<app-id>/{manifest.json,
#       docker-compose.yml.j2, icon.svg}, checks each manifest
#       (manifest.parse_manifest, and catalog/manifest.schema.json when
#       python3-jsonschema can load it), and keeps the result until a file
#       changes: an upgrade of nimbus-catalog is picked up on the next request,
#       without a restart. A broken app is left out and listed in .errors.
# Why:  docs/PLAN.md NOTE-60: catalog updates arrive with normal package
#       updates and never touch installed apps.
# =============================================================================

import json
import os

from .manifest import ManifestError, parse_manifest

FILES = ("manifest.json", "docker-compose.yml.j2", "icon.svg")


class Catalog:
    def __init__(self, root):
        self.root = root
        self._signature = None
        self._apps = {}
        self.errors = []

    def _current_signature(self):
        try:
            names = sorted(os.listdir(self.root))
        except FileNotFoundError:
            return ()
        sig = []
        for name in names:
            for file in FILES:
                try:
                    st = os.stat(os.path.join(self.root, name, file))
                    sig.append((name, file, st.st_mtime_ns, st.st_size))
                except (FileNotFoundError, NotADirectoryError):
                    pass
        try:
            sig.append(("", "manifest.schema.json", os.stat(os.path.join(self.root, "manifest.schema.json")).st_mtime_ns, 0))
        except FileNotFoundError:
            pass
        return tuple(sig)

    def _schema_validator(self):
        try:
            import jsonschema
            with open(os.path.join(self.root, "manifest.schema.json"), encoding="utf-8") as f:
                schema = json.load(f)
            return jsonschema.validators.validator_for(schema)(schema)
        except Exception:
            return None

    def _load(self):
        apps, errors = {}, []
        validator = self._schema_validator()
        try:
            names = sorted(os.listdir(self.root))
        except FileNotFoundError:
            names = []
        for name in names:
            folder = os.path.join(self.root, name)
            if not os.path.isdir(folder) or os.path.islink(folder):
                continue
            try:
                with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
                    raw = json.load(f)
                if validator is not None:
                    problem = next(iter(validator.iter_errors(raw)), None)
                    if problem is not None:
                        raise ManifestError(name, f"schema: {problem.message}")
                manifest = parse_manifest(raw, name)
                with open(os.path.join(folder, "docker-compose.yml.j2"), encoding="utf-8") as f:
                    template = f.read()
            except ManifestError as exc:
                errors.append(str(exc))
                continue
            except (OSError, ValueError) as exc:          # unreadable file, broken JSON
                errors.append(f"Catalog app '{name}': {exc}")
                continue
            icon = os.path.join(folder, "icon.svg")
            apps[name] = {"manifest": manifest, "template": template, "icon": icon if os.path.isfile(icon) else None}
        return apps, errors

    def _refresh(self):
        sig = self._current_signature()
        if sig != self._signature:
            self._apps, self.errors = self._load()
            self._signature = sig

    def apps(self):
        self._refresh()
        return self._apps

    def get(self, app_id):
        return self.apps().get(app_id)
