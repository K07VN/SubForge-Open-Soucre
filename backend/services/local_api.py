"""New status endpoint and explicit errors for unavailable online integrations."""
import urllib.request
import json


def add_local_routes(app):
    from fastapi import APIRouter
    from fastapi.responses import JSONResponse
    from services.local_ai_service import _cfg
    from routes import model_routes
    from routes import hardware_routes
    from recovery_runtime import CUDA_RUNTIME
    from pathlib import Path

    original_pack_installed = hardware_routes.is_gpu_pack_installed

    def local_pack_installed():
        # A verified wheel overlay remains installed when the user selects CPU.
        return original_pack_installed() or (
            (CUDA_RUNTIME / "verified.json").is_file()
            and not Path(hardware_routes.UNINSTALL_FLAG).exists()
        )

    hardware_routes.is_gpu_pack_installed = local_pack_installed

    router = APIRouter()

    @router.get("/api/public/announcements")
    def local_announcements():
        # The offline edition has no remote announcement feed.
        return {"success": True, "announcements": []}

    @router.get("/api/local/status")
    def local_status():
        base, model = _cfg()
        health = False
        try:
            with urllib.request.urlopen(base.removesuffix("/v1") + "/health", timeout=2) as r:
                health = r.status == 200
        except Exception:
            pass
        return {"edition": "local", "offline": True, "llm": {"model": model, "ready": health},
                "models": model_routes.get_models_status().model_dump(),
                "voice_migration": "Edge selections use a local voice of the same language; voice identity changes",
                "unavailable_offline": ["YouTube publishing/OAuth", "URL downloads", "web articles", "cloud voices"]}

    @app.middleware("http")
    async def online_feature_guard(request, call_next):
        path = request.url.path
        if (path.startswith("/api/youtube") and any(word in path for word in ("oauth", "upload", "publish", "sync", "auth"))) or path.startswith("/api/models/download"):
            return JSONResponse({"detail": "Tính năng này cần Internet. Bản local không kết nối dịch vụ bên ngoài; tải model bằng provision_models.py trước khi chạy."}, status_code=503)
        return await call_next(request)

    app.include_router(router)
    # The original root StaticFiles mount must remain last for the added API.
    mounts = [r for r in app.router.routes if getattr(r, "name", "") == "frontend"]
    for route in mounts:
        app.router.routes.remove(route)
        app.router.routes.append(route)
