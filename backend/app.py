"""InstaPI — FastAPI + WebSocket + aurora frontend."""
import sys
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse

PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import config as cfg
from backend.task_manager import TaskManager

app = FastAPI(title="InstaPI — Instagram Web API")
manager = TaskManager()

FRONT_DIR = PROJECT_ROOT / "frontend"
app.mount("/static", StaticFiles(directory=FRONT_DIR / "static"), name="static")


@app.on_event("startup")
async def _startup_refresh_ig_version():
    """Освежить x-instagram-ajax живым запросом при старте (best-effort, в потоке)."""
    import threading
    def _run():
        try:
            from web_api.ig_web_engine import refresh_ajax_version
            refresh_ajax_version(log=lambda m: print(f"[instapi] {m}"))
        except Exception:
            pass
    threading.Thread(target=_run, daemon=True).start()


# ==================== PAGES ====================

def _render(template: str):
    p = FRONT_DIR / "templates" / template
    return HTMLResponse(p.read_text(encoding="utf-8"))


@app.get("/")
async def index(): return _render("dashboard.html")

@app.get("/accounts")
async def page_accounts(): return _render("accounts.html")

@app.get("/zaliv")
async def page_zaliv(): return _render("zaliv.html")

@app.get("/stories")
async def page_stories(): return _render("stories.html")

@app.get("/sessions")
async def page_sessions(): return _render("sessions.html")

@app.get("/settings")
async def page_settings(): return _render("settings.html")


# ==================== API ====================

@app.get("/api/accounts")
async def api_accounts_list():
    return {"accounts": cfg.load_accounts()}


@app.post("/api/accounts/import")
async def api_accounts_import(body: dict):
    """Парсит text (user:pass:otp или user pass otp) и сохраняет."""
    text = (body.get("text") or "").strip()
    added = 0
    skipped = 0
    errors = []
    for line in text.split("\n"):
        line = line.strip()
        if not line: continue
        if ":" in line:
            parts = [p.strip() for p in line.split(":")]
        elif "\t" in line:
            parts = [p.strip() for p in line.split("\t")]
        else:
            parts = line.split()
        if len(parts) < 2:
            skipped += 1
            errors.append(f"bad: {line[:40]}")
            continue
        u, p = parts[0], parts[1]
        otp = parts[2].replace(" ", "") if len(parts) >= 3 else ""
        if u and p:
            cfg.upsert_account(u, p, otp)
            added += 1
        else:
            skipped += 1
    return {"ok": True, "added": added, "skipped": skipped, "errors": errors}


@app.delete("/api/accounts/{username}")
async def api_accounts_delete(username: str):
    cfg.delete_account(username)
    cfg.delete_web_session(username)
    return {"ok": True}


@app.get("/api/web-sessions")
async def api_sessions(): return {"sessions": cfg.list_web_sessions()}


@app.delete("/api/web-sessions/{username}")
async def api_delete_session(username: str):
    return {"ok": cfg.delete_web_session(username)}


@app.get("/api/web-proxies")
async def api_proxies():
    pp = cfg.load_autoreg_proxies()
    safe = [{"key": p["key"], "host": p["url"].split("@")[-1]} for p in pp]
    return {"proxies": safe, "count": len(pp)}


@app.get("/api/autoreg-config")
async def api_get_autoreg_config():
    return {"config": cfg.load_autoreg_config()}


@app.put("/api/autoreg-config")
async def api_put_autoreg_config(body: dict):
    cfg.save_autoreg_config(body.get("config", {}))
    return {"ok": True}


@app.get("/api/presets")
async def api_presets(): return {"presets": cfg.load_description_presets()}


@app.post("/api/presets")
async def api_add_preset(body: dict):
    name = (body.get("name") or "").strip()
    desc = body.get("description", "")
    if not name:
        raise HTTPException(400, "name required")
    cfg.add_description_preset(name, desc)
    return {"ok": True}


@app.delete("/api/presets/{name}")
async def api_delete_preset(name: str):
    cfg.delete_description_preset(name)
    return {"ok": True}


@app.get("/api/video-folders")
async def api_video_folders():
    return {"folders": cfg.get_video_subfolders()}


@app.get("/api/story-folders")
async def api_story_folders():
    return {"folders": cfg.get_story_subfolders()}


@app.get("/api/lost-accounts")
async def api_lost(): return {"lost": cfg.load_lost_accounts()}


@app.get("/api/zaliv-log")
async def api_zaliv_log(): return {"log": cfg.load_zaliv_log()[-500:]}


@app.get("/api/tasks")
async def api_tasks(): return {"tasks": manager.get_active_tasks()}


@app.get("/api/stats")
async def api_stats():
    accs = cfg.load_accounts()
    sess = cfg.list_web_sessions()
    log = cfg.load_zaliv_log()
    lost = cfg.load_lost_accounts()
    return {
        "accounts_total": len(accs),
        "sessions_total": len([s for s in sess if s.get("has_session")]),
        "uploads_total": len(log),
        "lost_total": len(lost),
    }


# ==================== WEBSOCKET ====================

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    manager.register(ws)
    try:
        while True:
            data = await ws.receive_json()
            t = data.get("type")
            if t == "zaliv_start":
                await manager.start_zaliv(ws, data)
            elif t == "story_start":
                await manager.start_story_zaliv(ws, data)
            elif t == "add_accounts_start":
                await manager.start_add_accounts(ws, data)
            elif t == "task_stop":
                manager.stop_task(data.get("task_id", ""))
            else:
                await ws.send_json({"type": "error", "message": f"Unknown: {t}"})
    except WebSocketDisconnect:
        manager.unregister(ws)
    except Exception:
        manager.unregister(ws)
