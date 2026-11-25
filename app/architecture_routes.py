"""Architecture Center routes inside the private steward (#91).

Every route uses the steward's own authorization (`allowed`), so there is no
second login, token or public path. Generated HTML is served only as a
sandboxed iframe document:

* CSP `sandbox allow-scripts` (no allow-same-origin): the document runs in an
  opaque origin, cannot read steward cookies/storage or call steward APIs with
  credentials; `connect-src 'none'` and no remote sources deny network access.
* The viewer page sets `frame-src 'self'`, so the frame cannot navigate itself
  to another origin (blocks exfiltration by navigation).
* The raw route answers only same-origin iframe loads (Sec-Fetch-*), so the
  generated JS never runs as a top-level page.
"""

from __future__ import annotations

import time
from datetime import datetime

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from app.services import architecture_runs as runs_mod
from app.services.architecture_center import (
    KINDS,
    PROJECT_RE,
    ArchitectureStore,
    ArtifactRejected,
    deployment_view,
    freshness,
    history,
)
from app.services.fleet_steward import NODES, VN

ARTIFACT_CSP = (
    "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline' blob:; worker-src blob:; "
    "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; media-src data: blob:; "
    "connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'self'"
)
VIEWER_CSP = "frame-src 'self'; frame-ancestors 'self'; form-action 'self'; base-uri 'self'"
TABS = (("overview", "Tổng quan"), ("archify", "Archify"), ("floorplan", "Floorplan"),
        ("history", "Lịch sử"), ("evidence", "Bằng chứng"))
STATE_TEXT = {
    "CURRENT": "Mới", "AGED": "Cùng commit nhưng đã lâu", "STALE": "Đã cũ", "UNKNOWN": "Không xác định",
    "MATCH": "Khớp", "DIFFERENT": "Khác", "PASS": "Đạt", "PARTIAL": "Một phần", "FAIL": "Không đạt", "NOT_RUN": "Chưa chạy",
}


def register(router: APIRouter, *, store, allowed, same_origin, render, private):
    def arch(memory):
        return ArchitectureStore(memory.path)

    def project_id(owner, repo):
        pid = f"{owner}/{repo}"
        if not PROJECT_RE.fullmatch(pid) or ".." in pid:
            raise HTTPException(404)
        return pid

    def guard(request):
        memory = store()
        if not allowed(request, memory):
            raise HTTPException(401, headers=private)
        return memory

    def stamp(t):
        if not t:
            return "Chưa có"
        if isinstance(t, datetime):
            return t.astimezone(VN).strftime("%d/%m/%Y %H:%M")
        return datetime.fromtimestamp(t, VN).strftime("%d/%m/%Y %H:%M")

    def config():
        try:
            return runs_mod.load_config(), None
        except (runs_mod.RunRefused, ValueError, OSError) as exc:
            return {"projects": {}, "tools": {}, "limits": {}}, str(exc)[:200]

    def observations(memory, project):
        out = []
        for n in NODES:
            at, body = memory.report(n, int(time.time()))
            for r in body.get("items", []):
                if r["project"] == project:
                    out.append(dict(r, node=n, at=at))
        return out

    @router.get("/steward/architecture")
    def fleet(request: Request):
        memory = store()
        if not allowed(request, memory):
            resp = render(request, "fleet_steward_login.html")
            resp.headers.update(private)
            return resp
        cfg, cfg_error = config()
        have = {p["project"]: p for p in arch(memory).projects()}
        names = sorted(set(have) | set(cfg.get("projects", {})))
        rows = [dict(project=p, snapshots=have.get(p, {}).get("n", 0), latest=have.get(p, {}).get("latest"),
                     configured=p in cfg.get("projects", {})) for p in names]
        resp = render(request, "architecture_center.html", page="fleet", rows=rows, stamp=stamp, cfg_error=cfg_error)
        resp.headers.update(private)
        return resp

    @router.get("/steward/architecture/{owner}/{repo}")
    def project_page(request: Request, owner: str, repo: str, tab: str = "overview", snapshot: str = "", msg: str = ""):
        memory = store()
        if not allowed(request, memory):
            resp = render(request, "fleet_steward_login.html")
            resp.headers.update(private)
            return resp
        pid = project_id(owner, repo)
        if tab not in dict(TABS):
            raise HTTPException(404)
        a = arch(memory)
        snaps = a.snapshots(pid)
        cfg, cfg_error = config()
        if snapshot:
            current = a.snapshot(pid, snapshot)
            if not current:
                raise HTTPException(404)
        else:
            current = snaps[0] if snaps else None
        tracked = runs_mod.tracked_revision(cfg, pid)
        fresh = dep = None
        if current:
            fresh = freshness(current, runs_mod.with_distance(tracked, current["source"]["commit"]))
            dep = deployment_view(current, observations(memory, pid))
        project_runs = runs_mod.runs(pid) if pid in cfg.get("projects", {}) else []
        resp = render(
            request, "architecture_center.html", page="project", pid=pid, owner=owner, repo=repo, tab=tab, tabs=TABS,
            snaps=snaps, current=current, fresh=fresh, dep=dep, hist=history(snaps) if tab == "history" else [],
            runs=project_runs, configured=pid in cfg.get("projects", {}), cfg_error=cfg_error,
            tracked={k: v for k, v in (tracked or {}).items() if k != "source"}, msg=msg[:300], stamp=stamp, label=STATE_TEXT,
            kinds=KINDS,
        )
        resp.headers.update(private)
        if tab in KINDS:
            resp.headers["Content-Security-Policy"] = VIEWER_CSP
        return resp

    @router.get("/steward/architecture/{owner}/{repo}/artifact/{snap_id}/{kind}")
    def artifact(request: Request, owner: str, repo: str, snap_id: str, kind: str, sha: str = ""):
        memory = guard(request)
        pid = project_id(owner, repo)
        # Only a same-origin iframe load may receive generated HTML; never a top-level page,
        # a cross-site embed or a script fetch.
        dest = request.headers.get("sec-fetch-dest")
        site = request.headers.get("sec-fetch-site")
        if dest is not None and (dest != "iframe" or site not in ("same-origin", None)):
            raise HTTPException(403, headers=private)
        try:
            data = arch(memory).artifact(pid, snap_id, kind, sha)
        except LookupError:
            raise HTTPException(404, headers=private) from None
        except ArtifactRejected as exc:
            raise HTTPException(409, str(exc), headers=private) from exc
        return Response(
            data,
            media_type="text/html; charset=utf-8",
            headers={
                **private,
                "Content-Security-Policy": ARTIFACT_CSP,
                "X-Content-Type-Options": "nosniff",
                "Cross-Origin-Resource-Policy": "same-origin",
                "X-Frame-Options": "SAMEORIGIN",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=(), usb=(), payment=(), clipboard-read=()",
                "Content-Disposition": "inline",
                "ETag": '"' + sha + '"',
            },
        )

    @router.post("/steward/architecture/{owner}/{repo}/regenerate")
    def regenerate(request: Request, owner: str, repo: str, commit: str = Form("")):
        same_origin(request)
        guard(request)
        pid = project_id(owner, repo)
        commit = commit.strip().lower()
        try:
            r = runs_mod.request_rerun(pid, commit or None)
            msg = r["message"]
        except runs_mod.RunRefused as exc:
            msg = "Từ chối: " + str(exc)
        from urllib.parse import quote

        return RedirectResponse(f"/steward/architecture/{owner}/{repo}?tab=overview&msg={quote(msg)}", 303, headers=private)
