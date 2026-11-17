import logging
from contextlib import asynccontextmanager
from functools import cache
from pathlib import Path

import psycopg
from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from starlette.middleware.sessions import SessionMiddleware

from . import auth, connector, hippo
from .db import pool
from .routers import sheets
from .settings import S, startup_problems

log = logging.getLogger("bomtree")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app):
    problems = startup_problems()
    if problems:   # refuse to serve production with settings that would let the wrong people in
        raise RuntimeError("Unsafe settings for " + S.ENVIRONMENT + ": " + "; ".join(problems))
    pool.open()
    yield
    pool.close()


app = FastAPI(title="BOMTree", lifespan=lifespan, docs_url=None, openapi_url=None, redoc_url=None)
app.add_middleware(SessionMiddleware, secret_key=S.SECRET_KEY, session_cookie="bomtree", same_site="lax",
                   https_only=S.ENVIRONMENT != "local", max_age=int(S.SESSION_HOURS * 3600))


@app.exception_handler(psycopg.Error)
def db_error(request: Request, e: psycopg.Error):
    """Database rules (BT4xx codes from file 2, checks) become clear answers; anything else is a logged 500."""
    msg = e.diag.message_primary or str(e)
    if isinstance(e, (psycopg.errors.RaiseException, psycopg.errors.CheckViolation, psycopg.errors.UniqueViolation)) \
            or (e.sqlstate or "").startswith("BT"):
        status = {"BT403": 403, "BT404": 404, "BT409": 409}.get(e.sqlstate, 422)
        return JSONResponse({"detail": {"code": e.sqlstate, "message": msg}}, status)
    log.error("database error on %s %s: %s", request.method, request.url.path, msg)
    return JSONResponse({"detail": {"code": "database", "message": "Database error"}}, 500)


api = APIRouter(prefix="/api/v1")
api.include_router(auth.router)
api.include_router(sheets.router)
app.include_router(api)
app.include_router(connector.router)   # /mcp, /oauth/*, /.well-known/*: the claude.ai connector
app.include_router(hippo.router)       # /hippo/mcp: the Hippo connector; /admin: the People page


@cache
def page():
    """The BOMTree page exactly as published in claude.ai, with the stand-in for window.claude put in first."""
    html = (STATIC / "bomtree.html").read_text("utf-8")
    return html.replace("<head>", '<head><script src="/bt-shim.js"></script>', 1)


@app.get("/")
def index(request: Request):
    # bomtree.html is the claude.ai page's copy; it holds internal Taraz data, so it isn't in the public repo and
    # only the bomtree-app service has it. Without it (the Hippo service) the site opens on the People page.
    if not (STATIC / "bomtree.html").exists():
        return RedirectResponse("/admin")
    if not request.session.get("uid"):
        return RedirectResponse("/signin")
    return HTMLResponse(page(), headers={"Cache-Control": "no-store"})


@app.get("/signin")
def signin():
    return FileResponse(STATIC / "signin.html")


@app.get("/{name}.js")
def script(name: str):
    if name not in ("bt-shim", "tos-launcher") or not (STATIC / f"{name}.js").exists():
        return JSONResponse({"detail": "Not found"}, 404)
    return FileResponse(STATIC / f"{name}.js", media_type="text/javascript", headers={"Cache-Control": "no-cache"})


@app.get("/health")
def health():
    with pool.connection() as conn:
        conn.execute("SELECT 1")
    return {"ok": True}


if S.DEV_LOGIN:   # local testing only (production refuses DEV_LOGIN): stands in for the claude.ai page store, see bt-shim.js
    DEVSTORE = {}

    @app.post("/dev/store")
    def dev_store(body: dict = Body(...)):
        op, path = body["op"], body.get("path", "")
        if op == "set":
            DEVSTORE[path] = body["data"]
        elif op == "delete":
            DEVSTORE.pop(path, None)
        elif op == "list":
            n = path.count("/") + 1
            return {"docs": [{"id": k.rsplit("/", 1)[1], "data": v} for k, v in DEVSTORE.items()
                             if k.startswith(path + "/") and k.count("/") == n]}
        return {"data": DEVSTORE.get(path)}
