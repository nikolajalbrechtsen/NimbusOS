# =============================================================================
# nimbus_control/app.py: the Control Center application
# =============================================================================
# What: create_app(settings, privd) builds the FastAPI app:
#         - state: settings, the per-boot token, launch codes, the privd
#           client, the fonts found in Debian's packages, the services, and
#           "counters" (name -> async function) for the sidebar counts;
#         - routes: session, system, updates, apps and the catalog, jobs,
#           VMs and the ISO library, storage, shares and SMB users, settings,
#           fonts, /logo.svg (the NimbusOS logo from
#           nimbusos-branding), the packaged noVNC at /novnc/ (VM consoles),
#           then the frontend's static files;
#         - no OpenAPI or docs pages (nothing to browse on a desktop app);
#         - everything wrapped in SecurityMiddleware (auth.py).
#       Returns the wrapped ASGI app, which nimbus-control-server runs with
#       uvicorn.
# Why:  spec section 3 (Architecture): one backend that also serves the
#       static frontend.
# =============================================================================

import asyncio
import os

import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import (apps_service, errors, fonts, jobs, shares_service, storage_service, system_service, updates_service,
               vm_service)
from .auth import LaunchCodes, SecurityMiddleware, TokenStore
from .auth import router as session_router
from .catalog import Catalog
from .config import Settings
from .docker_api import DockerApi
from .errors import ApiError
from .iso_library import IsoLibrary
from .libvirt_api import LibvirtApi
from .privd_client import PrivdClient
from .state import StateStore


def create_app(settings=None, privd=None, font_lister=fonts.package_files, docker=None, libvirt=None, bridges=None):
    settings = settings or Settings.from_env()
    app = FastAPI(title="Nimbus Control Center", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.tokens = TokenStore(settings.run_dir)
    app.state.codes = LaunchCodes()
    app.state.privd = privd or PrivdClient(settings.privd_socket)
    app.state.fonts = fonts.discover(font_lister)
    app.state.store = StateStore(settings.state_dir)
    app.state.catalog = Catalog(settings.catalog_dir)
    app.state.jobs = jobs.JobManager()
    app.state.updates = updates_service.UpdatesService(app.state.privd)
    app.state.apps = apps_service.AppsService(app.state.store, app.state.catalog, docker or DockerApi(),
                                              app.state.privd, app.state.jobs)
    app.state.isos = IsoLibrary(settings.isos_dir, settings.iso_max_bytes)
    app.state.vms = vm_service.VmService(libvirt or LibvirtApi(settings.libvirt_uri, settings.images_dir), app.state.isos,
                                         settings.vnc_tokens_dir, **({"bridges": bridges} if bridges else {}))
    app.state.shares = shares_service.SharesService(app.state.store, app.state.privd, app.state.jobs)
    app.state.storage = storage_service.StorageService(app.state.store, app.state.privd, app.state.jobs,
                                                       app.state.shares)
    # A fresh start: no console stays open and no upload can be resumed.
    app.state.vms.clear_tokens()
    try:
        app.state.isos.cleanup(everything=True)
    except OSError:
        pass

    async def app_count():
        return len(app.state.store.stacks())

    async def vm_count():
        return await asyncio.to_thread(app.state.vms.libvirt.count)

    async def share_count():
        return len(app.state.store.shares())

    app.state.counters = {"updates": app.state.updates.pending_count, "apps": app_count, "vms": vm_count,
                          "shares": share_count}
    psutil.cpu_percent(interval=None)        # the first reading is always 0.0

    errors.install(app)
    app.include_router(session_router)
    app.include_router(system_service.router)
    app.include_router(updates_service.router)
    app.include_router(apps_service.router)
    app.include_router(jobs.router)
    app.include_router(vm_service.router)
    app.include_router(storage_service.router)
    app.include_router(shares_service.router)
    app.include_router(fonts.router)

    @app.get("/logo.svg", include_in_schema=False)
    async def logo():
        if not os.path.isfile(settings.logo_path):
            raise ApiError(404, "The NimbusOS logo (nimbusos-branding) is not installed.")
        return FileResponse(settings.logo_path, media_type="image/svg+xml")

    if os.path.isdir(settings.novnc_dir):           # the packaged noVNC, for the VM consoles
        app.mount("/novnc", StaticFiles(directory=settings.novnc_dir), name="novnc")
    if os.path.isdir(settings.www_dir):
        app.mount("/", StaticFiles(directory=settings.www_dir, html=True), name="www")
    return SecurityMiddleware(app, app.state.tokens, settings.port, ws_ports=(settings.websockify_port,))
