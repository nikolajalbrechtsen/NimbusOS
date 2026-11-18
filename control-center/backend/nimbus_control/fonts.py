# =============================================================================
# nimbus_control/fonts.py: the UI fonts, served from Debian's font packages
# =============================================================================
# What: at startup, finds Inter (fonts-inter) and JetBrains Mono
#       (fonts-jetbrains-mono) through the packages' own file lists
#       (dpkg-query -L), and serves the weights style.css uses at
#       /fonts/<key>: inter-400/500/600/700, mono-400/500. Nothing is copied
#       into the Control Center, and a font moving to another directory in a
#       Debian update is found again on the next start.
# Why:  docs/PLAN.md NOTE-13: no CDN, works offline, and the fonts' OFL
#       license stays with Debian's packages.
# =============================================================================

import os
import subprocess

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse

from .errors import ApiError

DPKG_QUERY = "/usr/bin/dpkg-query"
FONTS = {
    "inter-400": ("fonts-inter", "Inter-Regular.otf", "font/otf"),
    "inter-500": ("fonts-inter", "Inter-Medium.otf", "font/otf"),
    "inter-600": ("fonts-inter", "Inter-SemiBold.otf", "font/otf"),
    "inter-700": ("fonts-inter", "Inter-Bold.otf", "font/otf"),
    "mono-400": ("fonts-jetbrains-mono", "JetBrainsMono-Regular.ttf", "font/ttf"),
    "mono-500": ("fonts-jetbrains-mono", "JetBrainsMono-Medium.ttf", "font/ttf"),
}


def package_files(package):
    try:
        proc = subprocess.run([DPKG_QUERY, "-L", package], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return proc.stdout.splitlines() if proc.returncode == 0 else []


def discover(lister=package_files):
    found = {}
    listings = {}
    for key, (package, name, mime) in FONTS.items():
        files = listings.setdefault(package, lister(package))
        for path in files:
            if os.path.basename(path) == name and os.path.isfile(path):
                found[key] = (path, mime)
                break
    return found


router = APIRouter()


@router.get("/fonts/{key}")
async def font(key: str, request: Request):
    entry = request.app.state.fonts.get(key)
    if entry is None:
        raise ApiError(404, f"Font {key} is not available.")
    path, mime = entry
    return FileResponse(path, media_type=mime, headers={"cache-control": "public, max-age=86400"})
