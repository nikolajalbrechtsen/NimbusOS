#!/usr/bin/python3
# =============================================================================
# tools/releases-json.py: the release list published next to the APT repository
# =============================================================================
# What: writes releases.json, which nimbus-update downloads (with its
#       signature, made by tools/publish-apt.sh) to show release notes:
#         {"schema": 1, "releases": [
#            {"tag": "v0.2.0", "version": "0.2.0", "deb_version": "0.2.0",
#             "channel": "stable", "date": "2026-11-23T10:00:00Z",
#             "restart_required": false, "notes": "...", "url": "https://..."},
#            ...]}
#       newest first (Debian version order). Commands:
#         empty --out FILE      an empty list (the first setup)
#         add   --current FILE --out FILE --tag TAG --version V --deb-version V
#               --channel stable|beta --notes FILE --url URL [--date ISO]
#               adds TAG, or replaces it if the release is published again.
#       The notes come from docs/release-notes/<tag>.md (docs/PLAN.md
#       NOTE-37); a line "Restart-Required: yes" there sets restart_required
#       and is left out of the notes.
# Why:  spec 4.2 (release metadata) and NOTE-38: the Control Center shows
#       release notes without calling the GitHub API.
#
# Usage: see What. Needs python3 and dpkg (for the version order).
# =============================================================================

import argparse
import datetime
import functools
import json
import re
import subprocess
import sys

TAG_RE = re.compile(r"v\d+\.\d+\.\d+(-beta\.\d+)?")
RESTART_RE = re.compile(r"^Restart-Required:\s*(yes|no)\s*$", re.I | re.M)


def compare(a, b):
    """Debian version order (dpkg --compare-versions)."""
    if a == b:
        return 0
    lower = subprocess.run(["dpkg", "--compare-versions", a, "lt", b], check=False).returncode == 0
    return -1 if lower else 1


def read_notes(path):
    with open(path, encoding="utf-8") as handle:
        text = handle.read()
    match = RESTART_RE.search(text)
    restart = bool(match and match.group(1).lower() == "yes")
    notes = RESTART_RE.sub("", text).strip()
    if not notes:
        raise ValueError(f"{path} has no release notes besides the Restart-Required line")
    return notes, restart


def write(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1, ensure_ascii=False, sort_keys=True)
        handle.write("\n")


def main(argv):
    parser = argparse.ArgumentParser(description="Maintain releases.json (see the file header).")
    sub = parser.add_subparsers(dest="command", required=True)
    empty = sub.add_parser("empty")
    empty.add_argument("--out", required=True)
    add = sub.add_parser("add")
    for name in ("current", "out", "tag", "version", "deb-version", "channel", "notes", "url"):
        add.add_argument(f"--{name}", required=True)
    add.add_argument("--date", help="ISO 8601 UTC time (default: now)")
    args = parser.parse_args(argv)

    if args.command == "empty":
        write(args.out, {"schema": 1, "releases": []})
        return 0

    if not TAG_RE.fullmatch(args.tag):
        raise SystemExit(f"releases-json: {args.tag!r} is not a release tag (vX.Y.Z or vX.Y.Z-beta.N)")
    if args.channel not in ("stable", "beta"):
        raise SystemExit(f"releases-json: channel must be stable or beta, not {args.channel!r}")
    try:
        notes, restart = read_notes(args.notes)
    except (OSError, ValueError) as exc:
        raise SystemExit(f"releases-json: {exc}") from None
    with open(args.current, encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data.get("releases"), list):
        raise SystemExit(f"releases-json: {args.current} has no releases list")
    date = args.date or datetime.datetime.now(datetime.UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    entry = {"tag": args.tag, "version": args.version, "deb_version": args.deb_version, "channel": args.channel,
             "date": date, "restart_required": restart, "notes": notes, "url": args.url}
    releases = [r for r in data["releases"] if isinstance(r, dict) and r.get("tag") != args.tag] + [entry]
    releases.sort(key=functools.cmp_to_key(lambda a, b: compare(a["deb_version"], b["deb_version"])), reverse=True)
    write(args.out, {"schema": 1, "releases": releases})
    print(f"releases-json: {args.tag} ({args.deb_version}, {args.channel}{', restart required' if restart else ''}); "
          f"{len(releases)} releases listed")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
