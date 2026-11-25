from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from app.services.steward_ingress import ingest_bytes

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.services.steward_records import records
from app.services.fleet_steward import Memory, VN, read_view

COOKIE = "__Host-pf-steward"
PRIVATE = {
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
}


def register(app, render, current_user, db, decision):
    router = APIRouter()

    def store():
        filename = (
            os.getenv("PROJECTFLOW_STEWARD_DB")
            or Path.home() / ".local/state/projectflow-steward/memory.sqlite3"
        )
        if not filename or not Path(filename).is_file():
            raise HTTPException(503, "Quản gia chưa được cấu hình.")
        return Memory(filename)

    def allowed(request, memory):
        user = current_user(request)
        ids = os.getenv("PROJECTFLOW_STEWARD_OPERATOR_IDS", "").split(",")
        return bool(user and str(user["id"]) in ids) or memory.authorized(
            request.cookies.get(COOKIE)
        )

    def same_origin(request):
        origin = request.headers.get("origin")
        if request.headers.get("sec-fetch-site") == "cross-site" or (
            origin and origin != "https://" + request.headers["host"]
        ):
            raise HTTPException(403)

    def view(request, memory, q, node, project, when, intent):
        if max(map(len, (q, node, project, when, intent))) > 240:
            raise HTTPException(422)
        try:
            result = read_view(memory, q, node, project, when, intent)
            result["records"] = records(db, decision, result)
            return result
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @router.get("/steward")
    def page(
        request: Request,
        q: str = "",
        node: str = "",
        project: str = "",
        when: str = "",
        intent: str = "",
        page: int = 1,
    ):
        memory = store()
        if allowed(request, memory):
            data = view(request, memory, q, node, project, when, intent)
            total = len(data["items"])
            pages = max(1, (total + 59) // 60)
            page = min(max(1, page), pages)
            priority = {
                "android": 0,
                "phone-storage": 1,
                "control-path": 2,
                "systemd-user": 3,
                "docker": 4,
                "systemd-system": 5,
                "git": 6,
            }
            data["items"].sort(
                key=lambda r: (priority[r["source"]], r["node"], r["name"])
            )
            data["items"] = data["items"][(page - 1) * 60 : page * 60]
            resp = render(
                request,
                "fleet_steward.html",
                v=data,
                q=q,
                p=page,
                pages=pages,
                total=total,
                stamp=lambda t: (
                    datetime.fromtimestamp(t, VN).strftime("%d/%m %H:%M")
                    if t
                    else "Chưa đo"
                ),
            )
        else:
            resp = render(request, "fleet_steward_login.html")
        resp.headers.update(PRIVATE)
        return resp

    @router.post("/steward/login")
    def login(request: Request, code: str = Form(...)):
        same_origin(request)
        session = store().redeem(code.strip())
        if not session:
            raise HTTPException(403, "Mã đã hết hạn hoặc đã dùng.")
        resp = RedirectResponse("/steward", 303, headers=PRIVATE)
        resp.set_cookie(
            COOKIE,
            session,
            max_age=21 * 86400,
            secure=True,
            httponly=True,
            samesite="strict",
            path="/",
        )
        return resp

    @router.post("/steward/logout")
    def logout(request: Request):
        same_origin(request)
        store().revoke(request.cookies.get(COOKIE))
        resp = RedirectResponse("/steward", 303, headers=PRIVATE)
        resp.delete_cookie(
            COOKIE, path="/", secure=True, httponly=True, samesite="strict"
        )
        return resp

    @router.post("/api/steward/captures")
    async def receive(request: Request):
        memory = store()
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > 2_000_000:
                raise HTTPException(413)
            raw.extend(chunk)
        try:
            return await run_in_threadpool(
                ingest_bytes,
                memory,
                os.getenv("PROJECTFLOW_STEWARD_KEYS")
                or Path.home() / ".config/projectflow-steward/nodes.json",
                request.headers.get("x-pf-node", ""),
                request.headers.get("x-pf-signature", ""),
                bytes(raw),
            )
        except PermissionError as exc:
            raise HTTPException(403) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, "Invalid capture") from exc

    @router.get("/api/steward")
    def api(
        request: Request,
        q: str = "",
        node: str = "",
        project: str = "",
        when: str = "",
        intent: str = "",
    ):
        memory = store()
        if not allowed(request, memory):
            raise HTTPException(401, headers=PRIVATE)
        return JSONResponse(
            view(request, memory, q, node, project, when, intent), headers=PRIVATE
        )

    app.include_router(router)
