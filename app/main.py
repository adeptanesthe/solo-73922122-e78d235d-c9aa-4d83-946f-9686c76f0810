"""FastAPI entrypoint: prop-box dot-matrix recognition and registry API."""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__, demo, services
from .codes import GridError
from .db import init_db
from .schemas import BOX_EXAMPLES, SCAN_EXAMPLES, BoxCreate, ScanCreate

STATIC_DIR = Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if os.environ.get("SEED_DEMO", "").strip() in {"1", "true", "TRUE", "yes"}:
        demo.seed_demo()
    yield


app = FastAPI(
    title="巡展道具箱点阵牌识别 API",
    description=(
        "5×5 黑白点阵牌册 + 确认读数。四个旋转朝向中任一朝向在可见格上至多错 1 格即命中；"
        "扫码返回候选箱号，唯一命中才确认读数并编号，重扫同一格阵沿用编号。"
        "新牌若与旧箱完整码互命中、或被任一已确认读数命中，则 409 拒绝挂牌。\n\n"
        "**演示**：先调用 `POST /api/demo/seed`，再在下方 `POST /api/boxes` 选择 "
        "`demo_blocked_by_occluded_reading` 示例并 Execute，即可看到一条四角遮挡的"
        "已确认读数拦下新牌并返回冲突箱号与读数编号。"
    ),
    version=__version__,
    lifespan=lifespan,
    docs_url=None,  # served manually with self-hosted assets below
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


def _custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )
    # Attach named request examples for the /docs "Examples" dropdown.
    try:
        box_mt = schema["paths"]["/api/boxes"]["post"]["requestBody"]["content"]["application/json"]
        box_mt["examples"] = BOX_EXAMPLES
        scan_mt = schema["paths"]["/api/scans"]["post"]["requestBody"]["content"]["application/json"]
        scan_mt["examples"] = SCAN_EXAMPLES
    except (KeyError, TypeError):
        pass
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]


@app.get("/docs", include_in_schema=False)
def custom_swagger_ui() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url or "/openapi.json",
        title=f"{app.title} · Swagger UI",
        swagger_js_url="/static/vendor/swagger-ui-bundle.js",
        swagger_css_url="/static/vendor/swagger-ui.css",
    )


@app.exception_handler(GridError)
async def grid_error_handler(request: Request, exc: GridError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"error": "invalid_grid", "message": str(exc)})


@app.exception_handler(services.ConflictError)
async def conflict_handler(request: Request, exc: services.ConflictError) -> JSONResponse:
    return JSONResponse(status_code=409, content=exc.payload)


# ----------------------------------------------------------------- meta

@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def index() -> str:
    return """<!doctype html><html lang="zh"><head><meta charset="utf-8">
<title>巡展道具箱点阵牌识别 API</title>
<style>body{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:0 1rem;line-height:1.6}
code,pre{background:#f4f4f5;border-radius:6px}code{padding:.1rem .35rem}pre{padding:1rem;overflow:auto}
a.btn{display:inline-block;background:#2563eb;color:#fff;padding:.6rem 1.1rem;border-radius:8px;text-decoration:none;margin-right:.6rem}
</style></head><body>
<h1>巡展道具箱 · 点阵牌识别 API</h1>
<p><a class="btn" href="/docs">打开 /docs 交互演示</a><a class="btn" href="/health">健康检查</a></p>
<h2>30 秒演示（一条遮挡读数拦下新牌）</h2>
<ol>
<li><code>POST /api/demo/seed</code> — 登记 <code>BOX-DEMO-01</code>，并确认一条四角遮挡的读数。</li>
<li>在 <code>/docs</code> 打开 <code>POST /api/boxes</code>，选择
 <b>demo_blocked_by_occluded_reading</b> 示例并 Execute。</li>
<li>得到 <b>409</b>：<code>conflicting_box_numbers=["BOX-DEMO-01"]</code>，
 <code>related_reading_ids=["RD-0001"]</code>（编号以实际为准）。</li>
<li>用 <code>POST /api/scans</code> 的 <b>occluded_corners</b> 示例重扫：
 同一读数编号、<code>pinned=true</code>、只命中原箱。</li>
</ol>
<pre>docker compose up --build
# 然后浏览器打开 http://localhost:8000/docs</pre>
<p>主要接口：<code>POST /api/boxes</code> · <code>POST /api/scans</code> ·
<code>GET /api/boxes</code> · <code>GET /api/readings</code> ·
<code>GET /api/demo/scenario</code></p>
</body></html>"""


@app.get("/health", tags=["meta"])
def health() -> dict:
    stats = services.index_stats()
    return {"status": "ok", "version": __version__, "stats": stats}


# ---------------------------------------------------------------- boxes

@app.post("/api/boxes", status_code=201, tags=["boxes"], summary="登记新箱 / 挂牌（冲突则 409）")
def create_box(payload: BoxCreate) -> dict:
    return services.register_box(payload.grid, box_number=payload.box_number)


@app.get("/api/boxes", tags=["boxes"], summary="牌册：列出全部箱号与码")
def get_boxes() -> dict:
    boxes = services.list_boxes()
    return {"count": len(boxes), "boxes": boxes}


@app.get("/api/boxes/{box_number}", tags=["boxes"], summary="查看单个箱号")
def get_box_detail(box_number: str) -> JSONResponse:
    box = services.get_box(box_number)
    if box is None:
        return JSONResponse(status_code=404, content={"error": "not_found", "box_number": box_number})
    return JSONResponse(status_code=200, content=box)


# ---------------------------------------------------------------- scans

@app.post("/api/scans", tags=["scans"], summary="提交扫码格阵，返回候选箱号并在唯一命中时确认")
def create_scan(payload: ScanCreate) -> dict:
    return services.submit_scan(payload.grid, scanner_id=payload.scanner_id)


@app.get("/api/readings", tags=["readings"], summary="已确认读数列表")
def get_readings(limit: int = 200) -> dict:
    limit = max(1, min(limit, 10000))
    readings = services.list_readings(limit=limit)
    return {"count": len(readings), "readings": readings}


@app.get("/api/readings/{identifier}", tags=["readings"], summary="按编号（RD-0001 或数字 id）查看读数")
def get_reading_detail(identifier: str) -> JSONResponse:
    reading = services.get_reading(identifier)
    if reading is None:
        return JSONResponse(status_code=404, content={"error": "not_found", "reading_id": identifier})
    return JSONResponse(status_code=200, content=reading)


# ----------------------------------------------------------------- demo

@app.post("/api/demo/seed", tags=["demo"], summary="幂等写入演示箱与遮挡确认读数")
def demo_seed() -> dict:
    return demo.seed_demo()


@app.get("/api/demo/scenario", tags=["demo"], summary="演示脚本与当前相关读数编号")
def demo_scenario() -> dict:
    return demo.scenario_payload()


# ------------------------------------------------- compatibility aliases
# Unversioned / verb-style aliases for clients that guess common paths.

_BOX_PATHS = (
    "/boxes", "/box", "/api/box",
    "/register", "/api/register",
    "/boxes/register", "/api/boxes/register",
)
_SCAN_PATHS = (
    "/scans", "/scan", "/api/scan",
    "/recognize", "/api/recognize",
    "/scans/recognize", "/api/scans/recognize",
)
for _path in _BOX_PATHS:
    app.add_api_route(_path, create_box, methods=["POST"],
                      include_in_schema=False, status_code=201)
for _path in _SCAN_PATHS:
    app.add_api_route(_path, create_scan, methods=["POST"],
                      include_in_schema=False, status_code=200)
