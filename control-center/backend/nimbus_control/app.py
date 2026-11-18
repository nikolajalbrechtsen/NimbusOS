# =============================================================================
# nimbus_control/app.py: the Control Center application
# =============================================================================
# What: create_app(settings, privd) builds the FastAPI app:
#         - state: settings, the per-boot token, launch codes, the privd
#           client, the fonts found in Debian's packages, the services, and
#           "counters" (name -> async function) for the sidebar counts;
#         - routes: session, system, updates, fonts, /logo.svg (the NimbusOS
#           logo from nimbusos-branding), then the frontend's static files;
#         - no OpenAPI or docs pages (nothing to browse on a desktop app);
#         - everything wrapped in SecurityMiddleware (auth.py).
#       Returns the wrapped ASGI app, which nimbus-control-server runs with
#       uvicorn.
# Why:  spec section 3 (Architecture): one backend that also serves the
#       static frontend.
# =============================================================================

import os

import psutil
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import errors, fonts, system_service, updates_service
from .auth import LaunchCodes, SecurityMiddleware, TokenStore
from .auth import router as session_router
from .config import Settings
from .errors import ApiError
from .privd_client import PrivdClient


def create_app(settings=None, privd=None, font_lister=fonts.package_files):
    settings = settings or Settings.from_env()
    app = FastAPI(title="Nimbus Control Center", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.settings = settings
    app.state.tokens = TokenStore(settings.run_dir)
    app.state.codes = LaunchCodes()
    app.state.privd = privd or PrivdClient(settings.privd_socket)
    app.state.fonts = fonts.discover(font_lister)
    app.state.updates = updates_service.UpdatesService(app.state.privd)
    app.state.counters = {"updates": app.state.updates.pending_count}
    psutil.cpu_percent(interval=None)        # the first reading is always 0.0

    errors.install(app)
    app.include_router(session_router)
    app.include_router(system_service.router)
    app.include_router(updates_service.router)
    app.include_router(fonts.router)

    @app.get("/logo.svg", include_in_schema=False)
    async def logo():
        if not os.path.isfile(settings.logo_path):
            raise ApiError(404, "The NimbusOS logo (nimbusos-branding) is not installed.")
        return FileResponse(settings.logo_path, media_type="image/svg+xml")

    if os.path.isdir(settings.www_dir):
        app.mount("/", StaticFiles(directory=settings.www_dir, html=True), name="www")
    return SecurityMiddleware(app, app.state.tokens, settings.port)
