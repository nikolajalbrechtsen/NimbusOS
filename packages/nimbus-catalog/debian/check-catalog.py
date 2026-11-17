# =============================================================================
# debian/check-catalog.py: build-time check of the NimbusOS app catalog
# =============================================================================
# What: for CATALOG_DIR (default "catalog"), checks that:
#         - manifest.schema.json exists and is a valid JSON Schema;
#         - there is at least one app; every app is a directory named like
#           an app id (^[a-z0-9][a-z0-9-]{0,62}$) holding manifest.json,
#           docker-compose.yml.j2 and icon.svg, and nothing that is a
#           symbolic link;
#         - each manifest is a JSON object that passes the schema, has "id"
#           equal to its directory name, and has "template_version", a whole
#           number of at least 1 (not a string, not true/false).
#       Prints every problem, then exits 1 if there was any.
# Why:  docs/PLAN.md NOTE-60. "Template update available" compares an app's
#       template_version with the one an installed stack was created from;
#       a missing or malformed version would hide updates or invent them.
#
# Usage: python3 debian/check-catalog.py [CATALOG_DIR]
# =============================================================================

import json
import os
import re
import sys

APP_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
REQUIRED_FILES = ("manifest.json", "docker-compose.yml.j2", "icon.svg")
NOT_APPS = {"manifest.schema.json", "SCHEMA.md"}


def main(argv):
    catalog = argv[1] if len(argv) > 1 else "catalog"
    problems = []

    def problem(where, text):
        problems.append(f"{where}: {text}")

    try:
        import jsonschema
    except ImportError:
        print("check-catalog: python3-jsonschema is not installed", file=sys.stderr)
        return 2

    schema_path = os.path.join(catalog, "manifest.schema.json")
    validator = None
    try:
        with open(schema_path, encoding="utf-8") as f:
            schema = json.load(f)
        cls = jsonschema.validators.validator_for(schema)
        cls.check_schema(schema)
        validator = cls(schema)
    except FileNotFoundError:
        problem(schema_path, "missing")
    except (ValueError, jsonschema.exceptions.SchemaError) as e:
        problem(schema_path, f"not a valid JSON Schema: {e}")

    try:
        entries = sorted(os.listdir(catalog))
    except OSError as e:
        print(f"check-catalog: cannot read {catalog}: {e.strerror}", file=sys.stderr)
        return 2

    apps = 0
    for name in entries:
        path = os.path.join(catalog, name)
        if name in NOT_APPS:
            continue
        if os.path.islink(path) or not os.path.isdir(path):
            problem(path, "unexpected entry (apps are directories; only SCHEMA.md and manifest.schema.json may sit next to them)")
            continue
        if not APP_ID.fullmatch(name):
            problem(path, "directory name is not a valid app id")
            continue
        apps += 1
        for root, dirs, files in os.walk(path):
            for entry in dirs + files:
                if os.path.islink(os.path.join(root, entry)):
                    problem(os.path.join(root, entry), "symbolic links are not allowed")
        for required in REQUIRED_FILES:
            if not os.path.isfile(os.path.join(path, required)):
                problem(path, f"{required} is missing")

        manifest_path = os.path.join(path, "manifest.json")
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except FileNotFoundError:
            continue
        except ValueError as e:
            problem(manifest_path, f"not valid JSON: {e}")
            continue
        if not isinstance(manifest, dict):
            problem(manifest_path, "must be a JSON object")
            continue
        if manifest.get("id") != name:
            problem(manifest_path, f"\"id\" is {manifest.get('id')!r}, but the directory is {name!r}")
        version = manifest.get("template_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            problem(manifest_path, f"\"template_version\" must be a whole number of at least 1, got {version!r}")
        if validator is not None:
            for error in sorted(validator.iter_errors(manifest), key=lambda e: list(e.path)):
                location = "/".join(str(p) for p in error.path) or "(top level)"
                problem(manifest_path, f"schema: {location}: {error.message}")

    if apps == 0:
        problem(catalog, "no apps found")

    for p in problems:
        print(f"check-catalog: {p}", file=sys.stderr)
    if problems:
        print(f"check-catalog: {len(problems)} problem(s) in {catalog}", file=sys.stderr)
        return 1
    print(f"check-catalog: {apps} app(s) OK in {catalog}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
