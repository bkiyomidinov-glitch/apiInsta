"""Standalone storage для InstaPI.

Хранит:
- accounts.json        — список аккаунтов с паролями и OTP
- web_sessions/        — куки после успешного логина
- autoreg_config.json  — настройки прокси
- description_presets.json — готовые описания для постов
- lost_accounts.json   — заблокированные/проблемные
- zaliv_log.json       — история заливов (для статистики)
"""
import json
import logging
import time
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
WEB_SESSIONS_DIR = DATA_DIR / "web_sessions"
VIDEOS_DIR = BASE_DIR / "videos"
VIDEOS_DONE_DIR = BASE_DIR / "videos_done"
STORIES_DIR = BASE_DIR / "stories"
STORIES_DONE_DIR = BASE_DIR / "stories_done"

ACCOUNTS_FILE = DATA_DIR / "accounts.json"  # [{username,password,otp_secret,added_at}]
AUTOREG_CONFIG_FILE = DATA_DIR / "autoreg_config.json"
DESCRIPTION_PRESETS_FILE = DATA_DIR / "description_presets.json"
LOST_ACCOUNTS_FILE = DATA_DIR / "lost_accounts.json"
ZALIV_LOG_FILE = DATA_DIR / "zaliv_log.json"

for d in (DATA_DIR, WEB_SESSIONS_DIR, VIDEOS_DIR, VIDEOS_DONE_DIR,
          STORIES_DIR, STORIES_DONE_DIR):
    d.mkdir(parents=True, exist_ok=True)

# Медиа для сторис: фото + видео
STORY_IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp")
STORY_VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm")
STORY_MEDIA_EXTS = STORY_IMAGE_EXTS + STORY_VIDEO_EXTS


def _load_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"load {path.name}: {e}")
    return default


def _save_json(path: Path, data):
    try:
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"save {path.name}: {e}")


# ==================== ACCOUNTS ====================

def load_accounts() -> list:
    return _load_json(ACCOUNTS_FILE, [])


def save_accounts(accounts: list):
    _save_json(ACCOUNTS_FILE, accounts)


def upsert_account(username: str, password: str = "", otp_secret: str = ""):
    """Добавить или обновить аккаунт."""
    u = username.strip().lower().lstrip("@")
    if not u:
        return
    accs = load_accounts()
    for a in accs:
        if a.get("username") == u:
            if password:
                a["password"] = password
            if otp_secret:
                a["otp_secret"] = otp_secret.replace(" ", "")
            save_accounts(accs)
            return
    accs.append({
        "username": u,
        "password": password,
        "otp_secret": otp_secret.replace(" ", ""),
        "added_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    save_accounts(accs)


def delete_account(username: str):
    accs = load_accounts()
    accs = [a for a in accs if a.get("username") != username.lower()]
    save_accounts(accs)


def get_account(username: str) -> dict | None:
    for a in load_accounts():
        if a.get("username") == username.lower():
            return a
    return None


def get_otp_secret(username: str) -> str:
    a = get_account(username)
    return (a or {}).get("otp_secret", "")


# ==================== WEB SESSIONS ====================

def _web_session_path(username: str) -> Path:
    safe = "".join(c for c in username.lower() if c.isalnum() or c in "_.-") or "unknown"
    return WEB_SESSIONS_DIR / f"{safe}.json"


def save_web_session(username: str, cookies: dict, *, user_agent: str = "",
                     www_claim: str = "", proxy: str = "",
                     sec_ch_ua: str = "", sec_ch_ua_full: str = "",
                     platform_version: str = "", browser: str = ""):
    data = {
        "username": username,
        "cookies": cookies,
        "user_agent": user_agent,
        "www_claim": www_claim,
        "sec_ch_ua": sec_ch_ua,
        "sec_ch_ua_full": sec_ch_ua_full,
        "platform_version": platform_version,
        "browser": browser,
        "proxy": proxy,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "saved_ts": int(time.time()),
    }
    try:
        _web_session_path(username).write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error(f"save_web_session({username}): {e}")


def load_web_session(username: str) -> dict | None:
    p = _web_session_path(username)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error(f"load_web_session({username}): {e}")
        return None


def has_valid_web_session(username: str, *, max_age_days: int = 30) -> bool:
    data = load_web_session(username)
    if not data or not data.get("cookies", {}).get("sessionid"):
        return False
    age_sec = time.time() - data.get("saved_ts", 0)
    return age_sec < max_age_days * 86400


def delete_web_session(username: str) -> bool:
    p = _web_session_path(username)
    if p.exists():
        try:
            p.unlink()
            return True
        except Exception as e:
            logger.error(f"delete_web_session: {e}")
    return False


def list_web_sessions() -> list[dict]:
    out = []
    for f in WEB_SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out.append({
                "username": data.get("username", f.stem),
                "saved_at": data.get("saved_at", ""),
                "saved_ts": data.get("saved_ts", 0),
                "has_session": bool(data.get("cookies", {}).get("sessionid")),
                "proxy": (data.get("proxy") or "").split("@")[-1] if data.get("proxy") else "",
            })
        except Exception:
            continue
    out.sort(key=lambda x: x.get("saved_ts", 0), reverse=True)
    return out


# ==================== PROXIES ====================

def load_autoreg_config() -> dict:
    return _load_json(AUTOREG_CONFIG_FILE, {})


def save_autoreg_config(cfg: dict):
    _save_json(AUTOREG_CONFIG_FILE, cfg)


def _iter_proxy_entries(cfg: dict) -> list[tuple[str, dict]]:
    """Все прокси из конфига как (key, dict).

    Новый формат: cfg["proxies"] = [ {...}, {...}, ... ] — произвольное число.
    Легаси: отдельные ключи cfg["proxy"], cfg["proxy2"] (читаются, если списка нет).
    """
    entries = []
    proxies = cfg.get("proxies")
    if isinstance(proxies, list):
        for i, p in enumerate(proxies):
            if isinstance(p, dict):
                entries.append((f"proxy{i+1}", p))
    if not entries:
        for key in ("proxy", "proxy2"):
            p = cfg.get(key)
            if isinstance(p, dict):
                entries.append((key, p))
    return entries


def load_autoreg_proxies() -> list[dict]:
    """Список активных прокси: [{url, change_ip_url, key}, ...] (произвольное число)."""
    cfg = load_autoreg_config()
    out = []
    for key, p in _iter_proxy_entries(cfg):
        if not p.get("enabled") or not p.get("host") or not p.get("port"):
            continue
        host, port = p["host"], p["port"]
        user, pwd = p.get("username", ""), p.get("password", "")
        url = f"http://{user}:{pwd}@{host}:{port}" if user and pwd else f"http://{host}:{port}"
        out.append({"url": url, "change_ip_url": p.get("change_ip_url", ""), "key": key})
    return out


# ==================== DESCRIPTION PRESETS ====================

def load_description_presets() -> list:
    """[{name, description}, ...]"""
    return _load_json(DESCRIPTION_PRESETS_FILE, [])


def save_description_presets(presets: list):
    _save_json(DESCRIPTION_PRESETS_FILE, presets)


def add_description_preset(name: str, description: str):
    presets = load_description_presets()
    for p in presets:
        if p.get("name") == name:
            p["description"] = description
            save_description_presets(presets)
            return
    presets.append({"name": name, "description": description})
    save_description_presets(presets)


def delete_description_preset(name: str):
    presets = load_description_presets()
    presets = [p for p in presets if p.get("name") != name]
    save_description_presets(presets)


# ==================== LOST ACCOUNTS ====================

def log_lost_account(username: str, reason: str = "unknown"):
    items = _load_json(LOST_ACCOUNTS_FILE, [])
    now = int(time.time())
    # avoid dups within last 60s
    for it in items[-20:]:
        if it.get("username") == username and it.get("reason") == reason and (now - it.get("timestamp", 0)) < 60:
            return
    items.append({"username": username, "reason": reason, "timestamp": now,
                  "date": time.strftime("%Y-%m-%d %H:%M:%S")})
    _save_json(LOST_ACCOUNTS_FILE, items[-1000:])


def load_lost_accounts() -> list:
    return _load_json(LOST_ACCOUNTS_FILE, [])


# ==================== ZALIV LOG ====================

def append_zaliv_log_entry(entry: dict):
    log = _load_json(ZALIV_LOG_FILE, [])
    log.append(entry)
    _save_json(ZALIV_LOG_FILE, log[-5000:])


def load_zaliv_log() -> list:
    return _load_json(ZALIV_LOG_FILE, [])


# ==================== VIDEOS ====================

_video_lock = threading.Lock()
_reserved_videos = {}  # path -> task_id


def get_pending_videos() -> list:
    """Все видео из videos/ рекурсивно."""
    out = []
    if VIDEOS_DIR.exists():
        for p in VIDEOS_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
                out.append(str(p))
    return sorted(out)


def get_pending_videos_from_folder(folder: str) -> list:
    f = Path(folder)
    out = []
    if f.exists() and f.is_dir():
        for p in f.iterdir():
            if p.is_file() and p.suffix.lower() in (".mp4", ".mov", ".avi", ".mkv"):
                out.append(str(p))
    return sorted(out)


def get_video_subfolders() -> list:
    """Подпапки в videos/. [{name, path, count}]"""
    out = [{"name": "/ (все видео)", "path": str(VIDEOS_DIR), "count": len(get_pending_videos())}]
    if VIDEOS_DIR.exists():
        for d in sorted(VIDEOS_DIR.iterdir()):
            if d.is_dir():
                out.append({
                    "name": d.name, "path": str(d),
                    "count": len(get_pending_videos_from_folder(str(d))),
                })
    return out


def reserve_video(path: str, task_id: str) -> bool:
    with _video_lock:
        if path in _reserved_videos:
            return False
        _reserved_videos[path] = task_id
        return True


def release_video(path: str):
    with _video_lock:
        _reserved_videos.pop(path, None)


def release_videos_for_task(task_id: str):
    with _video_lock:
        to_remove = [p for p, t in _reserved_videos.items() if t == task_id]
        for p in to_remove:
            _reserved_videos.pop(p, None)


def get_unreserved_videos(video_list: list) -> list:
    with _video_lock:
        return [v for v in video_list if v not in _reserved_videos]


def move_video_to_done(video_path: str, source_folder: str | None = None):
    """Переместить видео в videos_done/ (с сохранением подкаталога)."""
    src = Path(video_path)
    if not src.exists():
        return
    try:
        # Определить куда в done: если видео было в под-папке videos/X/video.mp4 → videos_done/X/video.mp4
        rel_parent = ""
        try:
            rel = src.relative_to(VIDEOS_DIR)
            if len(rel.parts) > 1:
                rel_parent = str(Path(*rel.parts[:-1]))
        except ValueError:
            pass
        dst_dir = VIDEOS_DONE_DIR / rel_parent if rel_parent else VIDEOS_DONE_DIR
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        i = 1
        while dst.exists():
            dst = dst_dir / f"{src.stem}_{i}{src.suffix}"
            i += 1
        src.rename(dst)
    except Exception as e:
        logger.error(f"move_video_to_done: {e}")


# ==================== STORIES (медиа для сторис) ====================
# Резервацией/освобождением занимаются те же reserve_video/release_video —
# они работают по абсолютному пути и не привязаны к типу медиа.

def get_pending_stories() -> list:
    """Все медиа (фото+видео) из stories/ рекурсивно."""
    out = []
    if STORIES_DIR.exists():
        for p in STORIES_DIR.rglob("*"):
            if p.is_file() and p.suffix.lower() in STORY_MEDIA_EXTS:
                out.append(str(p))
    return sorted(out)


def get_pending_stories_from_folder(folder: str) -> list:
    f = Path(folder)
    out = []
    if f.exists() and f.is_dir():
        for p in f.iterdir():
            if p.is_file() and p.suffix.lower() in STORY_MEDIA_EXTS:
                out.append(str(p))
    return sorted(out)


def get_story_subfolders() -> list:
    """Подпапки в stories/. [{name, path, count}]"""
    out = [{"name": "/ (все медиа)", "path": str(STORIES_DIR), "count": len(get_pending_stories())}]
    if STORIES_DIR.exists():
        for d in sorted(STORIES_DIR.iterdir()):
            if d.is_dir():
                out.append({
                    "name": d.name, "path": str(d),
                    "count": len(get_pending_stories_from_folder(str(d))),
                })
    return out


def move_story_to_done(media_path: str, source_folder: str | None = None):
    """Переместить медиа сторис в stories_done/ (с сохранением подкаталога)."""
    src = Path(media_path)
    if not src.exists():
        return
    try:
        rel_parent = ""
        try:
            rel = src.relative_to(STORIES_DIR)
            if len(rel.parts) > 1:
                rel_parent = str(Path(*rel.parts[:-1]))
        except ValueError:
            pass
        dst_dir = STORIES_DONE_DIR / rel_parent if rel_parent else STORIES_DONE_DIR
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst = dst_dir / src.name
        i = 1
        while dst.exists():
            dst = dst_dir / f"{src.stem}_{i}{src.suffix}"
            i += 1
        src.rename(dst)
    except Exception as e:
        logger.error(f"move_story_to_done: {e}")


# ==================== UNIQUIFIERS (простые реализации) ====================

def uniquify_video_light(video_path: str, callback=None) -> str | None:
    """ffmpeg-lite: убрать metadata + пересжать, чтобы байты отличались."""
    import subprocess, tempfile
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            out_path = tf.name
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-map_metadata", "-1",
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k",
             out_path],
            capture_output=True, check=True, timeout=180,
        )
        return out_path
    except Exception as e:
        if callback: callback(f"  ⚠️ uniquify_light: {e}")
        return None


def uniquify_video_new(video_path: str, callback=None, hard: bool = False) -> str | None:
    """Расширенная уникализация: crop/scale + blur + EQ + pitch."""
    import subprocess, tempfile, random
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
            out_path = tf.name
        # небольшие случайные параметры
        hue = random.uniform(-0.05, 0.05)
        brightness = random.uniform(-0.03, 0.03)
        contrast = random.uniform(0.97, 1.03)
        vf = f"eq=brightness={brightness:.3f}:contrast={contrast:.3f},hue=h={hue*180:.1f}"
        if hard:
            vf += ",scale=iw*1.02:ih*1.02,crop=iw/1.02:ih/1.02"
        subprocess.run(
            ["ffmpeg", "-y", "-i", video_path,
             "-map_metadata", "-1",
             "-vf", vf,
             "-c:v", "libx264", "-preset", "fast", "-crf", "23",
             "-c:a", "aac", "-b:a", "128k",
             out_path],
            capture_output=True, check=True, timeout=300,
        )
        return out_path
    except Exception as e:
        if callback: callback(f"  ⚠️ uniquify_new: {e}")
        return None
