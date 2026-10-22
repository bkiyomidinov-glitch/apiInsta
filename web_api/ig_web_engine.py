"""HTTP-клиент Instagram Web API для логина и залива Reels.

Построен на основе захваченных API-запросов из Chrome DevTools:
- login: POST /api/v1/web/accounts/login/ajax/ (+ two_factor если нужно)
- upload: GET/POST rupload_igvideo + POST rupload_igphoto + POST /api/v1/media/configure_to_clips/
"""
import base64
import hashlib
import hmac
import io
import json
import logging
import os
import random
import re
import struct
import subprocess
import tempfile
import time
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

APP_ID = "936619743392459"
ASBD_ID = "359341"
# x-instagram-ajax = build IG web (__spin_r / rollout_hash). Протухает — самое
# частое устаревание. Освежить: refresh_ajax_version() (живой GET instagram.com).
# Значение от 2026-07-14 (проверено живым запросом).
AJAX_VERSION = "1043112633"
_ajax_version = AJAX_VERSION  # текущее (может быть обновлено refresh_ajax_version)
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)
DEFAULT_WWW_CLAIM = "0"

# Story canvas (IG web story format)
STORY_W = 1080
STORY_H = 1920


class WebLoginError(Exception):
    pass


class WebUploadError(Exception):
    pass


class WebChallengeError(WebUploadError):
    """Восстановимая ошибка загрузки — аккаунт НЕ удалять.

    Покрывает: checkpoint/challenge, feedback_required (action-block), spam,
    rate-limit и серверные 5xx. Во всех этих случаях аккаунт жив и требует
    ре-логина / ручной разблокировки / повторной попытки позже, а не удаления.
    """
    pass


_IG_CHALLENGE_KEYS = ("checkpoint_required", "challenge_required")


def classify_ig_block(status_code: int, text: str = "", j: dict | None = None) -> str:
    """Классифицирует ответ Instagram. Не бросает.

    Возвращает: '' (ok/неизвестно) | 'login_required' | 'challenge' | 'spam' | 'rate_limited'.
    Используется в session_status, configure_to_clips, upload-шагах.
    """
    body = text or ""
    low = body.lower()
    if j is None:
        try:
            j = json.loads(body) if body[:1] == "{" else {}
        except Exception:
            j = {}
    if not isinstance(j, dict):
        j = {}
    msg = str(j.get("message") or "").lower()

    if status_code in (401, 403) or msg == "login_required" or '"login_required"' in low:
        return "login_required"
    if (msg in _IG_CHALLENGE_KEYS or j.get("checkpoint_url")
            or '"checkpoint_required"' in low or '"challenge_required"' in low
            or '"checkpoint_url"' in low):
        return "challenge"
    if msg == "feedback_required" or j.get("feedback_required") or '"feedback_required"' in low:
        return "challenge"
    if j.get("spam"):
        return "spam"
    if status_code == 429 or "wait a few minutes" in low or "please wait a few" in low:
        return "rate_limited"
    return ""


def _raise_upload_error(stage: str, r) -> None:
    """Бросает типизированную ошибку для шага загрузки.

    Восстановимое (challenge/spam/rate-limit/серверный 5xx) → WebChallengeError
    (вызывающий код НЕ удаляет аккаунт). Протухшая сессия (401/403/login_required)
    → WebUploadError 'session invalid' (существующий путь). Прочее → WebUploadError.
    """
    cls = classify_ig_block(r.status_code, r.text)
    if cls in ("challenge", "spam", "rate_limited") or r.status_code >= 500:
        raise WebChallengeError(f"{stage}: recoverable ({r.status_code}, {cls or 'server_error'})")
    if cls == "login_required":
        raise WebUploadError(f"session invalid during {stage} ({r.status_code})")
    raise WebUploadError(f"{stage} failed {r.status_code}: {r.text[:200]}")


def totp(secret: str) -> str:
    """RFC6238 TOTP (SHA-1, 30s, 6 digits)."""
    secret = (secret or "").replace(" ", "").upper()
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + padding)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def _ffprobe_dims(video_path: Path) -> tuple[int, int, int]:
    """Return (width, height, duration_ms)."""
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,duration",
         "-of", "json", str(video_path)],
        capture_output=True, text=True, check=True,
    )
    s = json.loads(r.stdout)["streams"][0]
    w, h = int(s["width"]), int(s["height"])
    try:
        dur_ms = int(float(s["duration"]) * 1000)
    except (KeyError, ValueError):
        dur_ms = 6000
    return w, h, dur_ms


# ==================== LINK-СТИКЕР СТОРИС (визуал) ====================
# ВАЖНО: story_link_stickers в API создаёт только ТАП-ЗОНУ — визуала нет.
# Чтобы ссылка была ВИДНА, стикер вшивается в пиксели медиа (как в my_story_ext),
# а бокс тап-зоны выравнивается по реально отрисованному стикеру.
# Геометрия/иконка/варианты — 1:1 с расширением.

_CQW = STORY_W / 100  # 10.8 px

_STICKER_VARIANTS = {
    "default": {"bg": (255, 255, 255, 255), "icon": "#0097fd", "text": (0, 0, 0, 255)},
    "white":   {"bg": (255, 255, 255, 128), "icon": "#ffffff", "text": (255, 255, 255, 255)},
    "solid":   {"bg": (255, 255, 255, 255), "icon": "#0097fd", "text": (0, 151, 253, 255)},
    "black":   {"bg": (0, 0, 0, 255),       "icon": "#ffffff", "text": (255, 255, 255, 255)},
}

# SVG-путь иконки «звено цепи» (viewBox 24x24) — verbatim из my_story_ext.
_LINK_ICON_PATH = (
    "M12.7076 18.3639L11.2933 19.7781C9.34072 21.7308 6.1749 21.7308 4.22228 19.7781"
    "C2.26966 17.8255 2.26966 14.6597 4.22228 12.7071L5.63649 11.2929M18.3644 12.7071"
    "L19.7786 11.2929C21.7312 9.34024 21.7312 6.17441 19.7786 4.22179C17.826 2.26917 "
    "14.6602 2.26917 12.7076 4.22179L11.2933 5.636M8.50045 15.4999L15.5005 8.49994"
)

_STICKER_FONTS = ("/System/Library/Fonts/SFNS.ttf",
                  "/System/Library/Fonts/HelveticaNeue.ttc",
                  "/System/Library/Fonts/Supplemental/Arial.ttf")


def _sticker_metrics(scale: float) -> dict:
    """Метрики стикера — 1:1 с metrics() расширения."""
    return {
        "PAD": 2.5 * _CQW * scale,
        "ICON": 7.2 * _CQW * scale,
        "GAP_X": 2 * _CQW * scale,
        "GAP_EXTRA": 0.06 * _CQW * scale,
        "TEXT_DY": 0.5 * _CQW * scale,
        "R": 3.5 * scale / 100 * STORY_W,  # rx="3.5%" от ширины вьюпорта
    }


def render_link_sticker(text: str, *, scale: float = 1.0, variant: str = "default"):
    """Отрисовать link-стикер. Возвращает (RGBA-картинка, width_norm, height_norm).

    None при отсутствии зависимостей (Pillow/cairosvg) — тогда шлём только тап-зону.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception as e:
        logger.warning(f"link-стикер: нет Pillow ({e}) — визуал не вшит")
        return None

    v = _STICKER_VARIANTS.get(variant) or _STICKER_VARIANTS["default"]
    m = _sticker_metrics(scale)
    icon = int(round(m["ICON"]))

    font = None
    for fp in _STICKER_FONTS:
        try:
            font = ImageFont.truetype(fp, icon); break
        except Exception:
            continue
    if font is None:
        logger.warning("link-стикер: системный шрифт не найден")
        return None

    text_w = font.getlength(text)
    w_px = m["PAD"] + m["ICON"] + m["GAP_X"] + m["GAP_EXTRA"] + text_w + m["PAD"]
    h_px = m["PAD"] + m["ICON"] + m["TEXT_DY"] + m["PAD"]
    W, H = int(round(w_px)), int(round(h_px))

    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, W - 1, H - 1], radius=m["R"], fill=v["bg"])

    # Иконка — точный SVG-путь расширения через cairosvg.
    try:
        import cairosvg
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" '
               f'width="{icon}" height="{icon}"><path d="{_LINK_ICON_PATH}" fill="none" '
               f'stroke="{v["icon"]}" stroke-width="2" stroke-linecap="round" '
               f'stroke-linejoin="round"/></svg>')
        png = cairosvg.svg2png(bytestring=svg.encode(), output_width=icon, output_height=icon)
        ico = Image.open(io.BytesIO(png)).convert("RGBA")
        img.alpha_composite(ico, (int(round(m["PAD"])), int(round(m["PAD"]))))
    except Exception as e:
        logger.debug(f"link-стикер: иконка не отрисована ({e})")

    d.text((m["PAD"] + m["ICON"] + m["GAP_X"] + m["GAP_EXTRA"], m["PAD"] + m["TEXT_DY"]),
           text, font=font, fill=v["text"], anchor="lt")
    return img, W / STORY_W, H / STORY_H


def _sticker_xy(sticker, x: float, y: float) -> tuple[int, int]:
    """Левый-верхний угол стикера из центра (x,y) в нормализованных координатах."""
    return (int(round(x * STORY_W - sticker.width / 2)),
            int(round(y * STORY_H - sticker.height / 2)))


def _bake_link_on_photo(jpeg_bytes: bytes, sticker, x: float, y: float) -> bytes:
    """Вшить стикер в фото-сторис (1080×1920). Возвращает новый JPEG."""
    from PIL import Image
    base = Image.open(io.BytesIO(jpeg_bytes)).convert("RGBA")
    base.alpha_composite(sticker, _sticker_xy(sticker, x, y))
    out = io.BytesIO()
    base.convert("RGB").save(out, format="JPEG", quality=92)
    return out.getvalue()


def _bake_link_on_video(video_path: Path, sticker, x: float, y: float) -> Path | None:
    """Наложить стикер на видео-сторис через ffmpeg overlay. Новый файл или None."""
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
        png_path = Path(tf.name)
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        sticker.save(png_path, format="PNG")
        px, py = _sticker_xy(sticker, x, y)
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(video_path), "-i", str(png_path),
             "-filter_complex", f"[0:v][1:v]overlay={px}:{py}:format=auto",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "copy", str(out_path)],
            capture_output=True, check=True, timeout=300,
        )
        return out_path
    except Exception as e:
        logger.warning(f"link-стикер на видео: {e}")
        out_path.unlink(missing_ok=True)
        return None
    finally:
        png_path.unlink(missing_ok=True)


def _transcode_sleep() -> float:
    """Пауза между попытками configure при 'Transcode not finished'.

    Реальный браузер (HAR) ждал ~6с и одну попытку. Ровная 5.000с в цикле до 8×
    — робо-сигнал; даём джиттер 5-7с (центр ~6с как в HAR).
    """
    return random.uniform(5.0, 7.0)


def _extract_cover(video_path: Path, out_jpg: Path) -> None:
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video_path), "-vframes", "1",
         "-q:v", "2", str(out_jpg)],
        capture_output=True, check=True,
    )


def _story_photo_bytes(image_path: Path) -> tuple[bytes, int, int]:
    """Впечатать любое фото в холст сторис 1080×1920 (contain на чёрном фоне).

    Возвращает (jpeg_bytes, 1080, 1920). Логика повторяет bakeStoryPhoto из
    расширения my_story_ext: scale с сохранением пропорций + pad до 9:16.
    """
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
        out_path = Path(tf.name)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(image_path),
             "-vf", f"scale={STORY_W}:{STORY_H}:force_original_aspect_ratio=decrease,"
                    f"pad={STORY_W}:{STORY_H}:(ow-iw)/2:(oh-ih)/2:color=black",
             "-frames:v", "1", "-q:v", "2", str(out_path)],
            capture_output=True, check=True, timeout=60,
        )
        return out_path.read_bytes(), STORY_W, STORY_H
    finally:
        out_path.unlink(missing_ok=True)


# ==================== TLS-IMPERSONATE ТРАНСПОРТ (JA3/JA4 + HTTP/2) ====================
# httpx даёт Python/OpenSSL TLS-отпечаток (JA4 ...h1_, HTTP/1.1) — палится как бот.
# curl_cffi с impersonate отдаёт НАСТОЯЩИЙ Chrome JA3/JA4 + HTTP/2.
# Флаг-килсвитч: IG_NO_IMPERSONATE=1 → откат на httpx.
_USE_IMPERSONATE = os.environ.get("IG_NO_IMPERSONATE", "") != "1"

# TLS-таргет. UA-пул на Chrome 150 (реальный браузер юзера, по HAR), но curl_cffi
# 0.15 умеет JA3/JA4 максимум chrome146. ClientHello Chrome стабилен 146↔150 —
# JA3 совпадает, версия в UA детектором из JA3 не выводится. Когда curl_cffi
# добавит chrome150 — сменить сюда. Требует curl_cffi>=0.15.
_IMPERSONATE_TARGET = "chrome146"


class _NV:
    __slots__ = ("name", "value")
    def __init__(self, n, v):
        self.name = n; self.value = v


class _ImpersonateClient:
    """curl_cffi с интерфейсом httpx.Client (подмножество, нужное IgWebClient):
    .get/.post(headers, cookies, content=/data=, params, timeout), .close(),
    .cookies.jar (объекты с .name/.value), .event_hooks['response'].
    Транслирует httpx-стиль вызовов в curl_cffi, давая реальный JA3/JA4 + h2."""

    class _Cookies:
        def __init__(self, sess):
            self._sess = sess

        @property
        def jar(self):
            out = []
            c = getattr(self._sess, "cookies", None)
            if c is None:
                return out
            try:
                for name, value in c.items():
                    if name and value:
                        out.append(_NV(name, value))
                if out:
                    return out
            except Exception:
                pass
            try:
                for ck in getattr(c, "jar", []):
                    if getattr(ck, "name", None) and getattr(ck, "value", None):
                        out.append(_NV(ck.name, ck.value))
            except Exception:
                pass
            return out

    # curl_cffi impersonate — навигационный профиль: добавляет
    # upgrade-insecure-requests и sec-fetch-user, которых у реального fetch()/XHR
    # НЕТ. None → curl_cffi удаляет заголовок из своего дефолта.
    _STRIP_NAV = {"upgrade-insecure-requests": None, "sec-fetch-user": None}

    def __init__(self, impersonate, proxy=None, timeout=180.0):
        from curl_cffi import requests as _cffi
        kw = {"impersonate": impersonate, "timeout": timeout, "allow_redirects": True}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        self._sess = _cffi.Session(**kw)
        self.event_hooks = {"response": []}
        self.cookies = self._Cookies(self._sess)

    def _hdrs(self, headers):
        """Заголовки запроса + удаление навигационных дефолтов curl_cffi."""
        h = dict(headers or {})
        h.update(self._STRIP_NAV)
        return h

    def _fire(self, r):
        for h in self.event_hooks.get("response", []):
            try: h(r)
            except Exception: pass
        return r

    def get(self, url, headers=None, cookies=None, params=None, timeout=None):
        kw = {"headers": self._hdrs(headers), "cookies": cookies}
        if params is not None: kw["params"] = params
        if timeout is not None: kw["timeout"] = timeout
        return self._fire(self._sess.get(url, **kw))

    def post(self, url, headers=None, cookies=None, data=None, content=None,
             params=None, timeout=None):
        body = content if content is not None else data
        kw = {"headers": self._hdrs(headers), "cookies": cookies}
        if body is not None: kw["data"] = body
        if params is not None: kw["params"] = params
        if timeout is not None: kw["timeout"] = timeout
        return self._fire(self._sess.post(url, **kw))

    def close(self):
        try: self._sess.close()
        except Exception: pass


# ==================== ACCEPT-LANGUAGE ПО ГЕО ПРОКСИ ====================
# Accept-Language должен совпадать с гео egress-IP (прокси), иначе рассинхрон
# «язык ≠ страна IP» — сигнал бота. Определяем страну через прокси и маппим.

DEFAULT_ACCEPT_LANGUAGE = "en-US,en;q=0.9"

_ACCEPT_LANG_BY_CC = {
    "US": "en-US,en;q=0.9",           "GB": "en-GB,en;q=0.9",
    "IE": "en-IE,en;q=0.9",           "AU": "en-AU,en;q=0.9",
    "CA": "en-CA,en;q=0.9,fr;q=0.8",  "NZ": "en-NZ,en;q=0.9",
    "DE": "de-DE,de;q=0.9,en;q=0.8",  "AT": "de-AT,de;q=0.9,en;q=0.8",
    "CH": "de-CH,de;q=0.9,en;q=0.8",  "FR": "fr-FR,fr;q=0.9,en;q=0.8",
    "ES": "es-ES,es;q=0.9,en;q=0.8",  "IT": "it-IT,it;q=0.9,en;q=0.8",
    "PT": "pt-PT,pt;q=0.9,en;q=0.8",  "BR": "pt-BR,pt;q=0.9,en;q=0.8",
    "NL": "nl-NL,nl;q=0.9,en;q=0.8",  "BE": "nl-BE,nl;q=0.9,fr;q=0.8,en;q=0.7",
    "PL": "pl-PL,pl;q=0.9,en;q=0.8",  "UA": "uk-UA,uk;q=0.9,en;q=0.8",
    "CZ": "cs-CZ,cs;q=0.9,en;q=0.8",  "SK": "sk-SK,sk;q=0.9,en;q=0.8",
    "RO": "ro-RO,ro;q=0.9,en;q=0.8",  "HU": "hu-HU,hu;q=0.9,en;q=0.8",
    "BG": "bg-BG,bg;q=0.9,en;q=0.8",  "GR": "el-GR,el;q=0.9,en;q=0.8",
    "SE": "sv-SE,sv;q=0.9,en;q=0.8",  "NO": "nb-NO,nb;q=0.9,en;q=0.8",
    "DK": "da-DK,da;q=0.9,en;q=0.8",  "FI": "fi-FI,fi;q=0.9,en;q=0.8",
    "TR": "tr-TR,tr;q=0.9,en;q=0.8",  "MX": "es-MX,es;q=0.9,en;q=0.8",
    "AR": "es-AR,es;q=0.9,en;q=0.8",  "ID": "id-ID,id;q=0.9,en;q=0.8",
    "IN": "en-IN,en;q=0.9,hi;q=0.8",  "JP": "ja-JP,ja;q=0.9,en;q=0.8",
    "KR": "ko-KR,ko;q=0.9,en;q=0.8",  "KZ": "kk-KZ,kk;q=0.9,ru;q=0.8,en;q=0.7",
    "RU": "ru-RU,ru;q=0.9,en;q=0.8",
}

_geo_lang_cache = {}          # proxy_key -> (accept_language, ts)
_GEO_CACHE_TTL = 3600.0       # гео прокси стабильно — кэшируем на час


def _detect_country_code(proxy: str | None, timeout: float) -> str:
    """Страна egress-IP (ISO-2) через прокси. '' при неудаче. Не бросает."""
    kw = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        try:
            c = httpx.Client(proxy=proxy, **kw)
        except TypeError:
            c = httpx.Client(proxies=proxy, **kw)
    else:
        c = httpx.Client(**kw)
    try:
        for url, pick in (
            ("https://ipinfo.io/json", lambda j: j.get("country")),
            ("http://ip-api.com/json/?fields=countryCode", lambda j: j.get("countryCode")),
            ("https://ipapi.co/json/", lambda j: j.get("country_code")),
        ):
            try:
                r = c.get(url)
                if r.status_code == 200:
                    cc = (pick(r.json()) or "").upper()
                    if len(cc) == 2 and cc.isalpha():
                        return cc
            except Exception:
                continue
    finally:
        c.close()
    return ""


def accept_language_for_proxy(proxy: str | None = None, *, timeout: float = 10.0,
                              log=None) -> str:
    """Accept-Language по гео прокси (кэш по URL прокси, TTL 1ч).

    Прокси нет → гео прямого IP. Неизвестная страна/ошибка → дефолт en-US.
    """
    key = proxy or "__direct__"
    now = time.time()
    hit = _geo_lang_cache.get(key)
    if hit and (now - hit[1]) < _GEO_CACHE_TTL:
        return hit[0]
    cc = _detect_country_code(proxy, timeout)
    lang = _ACCEPT_LANG_BY_CC.get(cc, DEFAULT_ACCEPT_LANGUAGE) if cc else DEFAULT_ACCEPT_LANGUAGE
    if log:
        try: log(f"🌍 гео прокси: {cc or '??'} → Accept-Language: {lang.split(',')[0]}")
        except Exception: pass
    _geo_lang_cache[key] = (lang, now)
    return lang


# ==================== ВЕРСИЯ ВЕБ-API (x-instagram-ajax) ====================

def fetch_ig_web_version(proxy: str | None = None, *, timeout: float = 20.0) -> dict | None:
    """Живой GET instagram.com → текущие версии веб-API.

    Возвращает {'ajax','rollout_hash','app_id'} или None. curl_cffi (chrome) с
    фолбэком на httpx. ajax = __spin_r / rollout_hash (то, что идёт в x-instagram-ajax).
    """
    html = ""
    try:
        if _USE_IMPERSONATE:
            try:
                from curl_cffi import requests as _cffi
                kw = {"impersonate": _IMPERSONATE_TARGET, "timeout": timeout,
                      "allow_redirects": True}
                if proxy:
                    kw["proxies"] = {"http": proxy, "https": proxy}
                s = _cffi.Session(**kw)
                try:
                    html = s.get("https://www.instagram.com/").text
                finally:
                    s.close()
            except Exception:
                html = ""
        if not html:
            ckw = {"timeout": timeout, "follow_redirects": True}
            if proxy:
                try: c = httpx.Client(proxy=proxy, **ckw)
                except TypeError: c = httpx.Client(proxies=proxy, **ckw)
            else:
                c = httpx.Client(**ckw)
            try:
                html = c.get("https://www.instagram.com/").text
            finally:
                c.close()
    except Exception as e:
        logger.debug(f"fetch_ig_web_version: {e}")
        return None

    def g(*pats):
        for p in pats:
            m = re.search(p, html)
            if m:
                return m.group(1)
        return None

    ajax = g(r'"__spin_r":(\d+)', r'"rollout_hash":"(\d+)"', r'"server_revision":(\d+)')
    if not ajax:
        return None
    return {
        "ajax": ajax,
        "rollout_hash": g(r'"rollout_hash":"([^"]+)"') or ajax,
        "app_id": g(r'"X-IG-App-ID":"(\d+)"', r'"appId":"(\d+)"') or APP_ID,
    }


def refresh_ajax_version(proxy: str | None = None, *, log=None) -> str:
    """Обновить x-instagram-ajax из живого запроса. Возвращает актуальное значение.

    При неудаче/невалидном ответе сохраняет запечённое значение (безопасно).
    """
    global _ajax_version
    info = fetch_ig_web_version(proxy)
    ajax = (info or {}).get("ajax") or ""
    if ajax.isdigit() and len(ajax) >= 8:
        msg = (f"🔄 x-instagram-ajax: {_ajax_version} → {ajax}"
               if ajax != _ajax_version else f"✓ x-instagram-ajax актуален: {ajax}")
        _ajax_version = ajax
    else:
        msg = f"⚠️ x-instagram-ajax не обновлён, оставляю {_ajax_version}"
    if log:
        try: log(msg)
        except Exception: pass
    else:
        logger.info(msg)
    return _ajax_version


class IgWebClient:
    """Stateful HTTP client for Instagram web API.

    Usage:
        client = IgWebClient(cookies_dict)  # или await login(...)
        client.upload_reel(video_path, caption)
    """

    def __init__(
        self,
        cookies: dict | None = None,
        user_agent: str = DEFAULT_UA,
        www_claim: str = DEFAULT_WWW_CLAIM,
        sec_ch_ua: str = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"',
        sec_ch_ua_full: str = ('"Not;A=Brand";v="8.0.0.0", "Chromium";v="150.0.7871.115", '
                               '"Google Chrome";v="150.0.7871.115"'),
        platform_version: str = '"15.4.1"',
        accept_language: str = "en-US,en;q=0.9",
        timeout: float = 180.0,
        proxy: str | None = None,
    ):
        self.cookies = dict(cookies or {})
        self.user_agent = user_agent
        self.www_claim = www_claim
        self.sec_ch_ua = sec_ch_ua
        self.sec_ch_ua_full = sec_ch_ua_full
        # sec-ch-ua-platform-version реального Chrome — с точками ("15.2.0"),
        # подчёркивания только в замороженном UA-токене. Чиним легаси-формат.
        self.platform_version = (platform_version or '"15.2.0"').replace("_", ".")
        # Accept-Language аккаунта (из сессии); должен совпадать с гео IP.
        self.accept_language = accept_language or "en-US,en;q=0.9"
        # x-web-session-id — стабильный на сессию (3 base36-сегмента через ':').
        _a = "0123456789abcdefghijklmnopqrstuvwxyz"
        self.web_session_id = ":".join(
            "".join(random.choices(_a, k=6)) for _ in range(3))
        self._fb_dtsg_cache = None  # ленивая экстракция fb_dtsg (см. _fetch_fb_dtsg)
        self.proxy = proxy
        # Транспорт: curl_cffi impersonate=chrome (реальный Chrome JA3/JA4 + HTTP/2),
        # иначе httpx-фолбэк. Пул desktop-Chrome → всегда "chrome" (без iOS-ветки).
        self.transport = "httpx"
        self._client = None
        if _USE_IMPERSONATE:
            try:
                self._client = _ImpersonateClient(_IMPERSONATE_TARGET, proxy=proxy, timeout=timeout)
                self.transport = f"curl_cffi:{_IMPERSONATE_TARGET}"
            except Exception as _e:
                logger.warning(f"impersonate transport unavailable ({_e}) → httpx fallback")
                self._client = None
        if self._client is None:
            client_kwargs = {"http2": False, "follow_redirects": True, "timeout": timeout}
            if proxy:
                # httpx < 0.28: proxies=; >= 0.28: proxy=
                try:
                    client_kwargs["proxy"] = proxy
                    self._client = httpx.Client(**client_kwargs)
                except TypeError:
                    client_kwargs.pop("proxy", None)
                    client_kwargs["proxies"] = proxy
                    self._client = httpx.Client(**client_kwargs)
            else:
                self._client = httpx.Client(**client_kwargs)

        # Захват per-session x-ig-www-claim из любого ответа. Стартуем с "0",
        # дальше используем реальный, выданный этой сессии.
        try:
            self._client.event_hooks["response"].append(self._capture_www_claim)
        except Exception:
            pass

    def _capture_www_claim(self, response) -> None:
        try:
            v = (response.headers.get("x-ig-set-www-claim")
                 or response.headers.get("ig-set-x-ig-www-claim"))
            if v and v != self.www_claim:
                self.www_claim = v
        except Exception:
            pass

    def _fetch_fb_dtsg(self) -> str:
        """Извлечь fb_dtsg с главной страницы (нужна логин-сессия). Кэш на клиент.

        Возвращает токен формата NA…:digits ТОЛЬКО при строгой валидации формата;
        иначе '' (тогда configure шлётся без fb_dtsg/jazoest — рабочее поведение).
        Неверный токен ломает залив, поэтому паттерн-мисматч → пустая строка.
        """
        if self._fb_dtsg_cache is not None:
            return self._fb_dtsg_cache
        self._fb_dtsg_cache = ""
        try:
            r = self._client.get(
                "https://www.instagram.com/",
                headers=self._base_headers("https://www.instagram.com/", "GET"),
                cookies=self.cookies, timeout=20)
            html = r.text or ""
            for pat in (r'\["DTSGInitialData",\[\],\{"token":"([^"]+)"',
                        r'"dtsg":\{"token":"([^"]+)"',
                        r'name="fb_dtsg"\s+value="([^"]+)"'):
                m = re.search(pat, html)
                if m and re.fullmatch(r'NA[\w-]{20,}:[\d:]{6,}', m.group(1)):
                    self._fb_dtsg_cache = m.group(1)
                    break
        except Exception as e:
            logger.debug(f"fetch_fb_dtsg: {e}")
        return self._fb_dtsg_cache

    def _apply_dtsg(self, data: dict) -> None:
        """Дописать в тело configure fb_dtsg + jazoest (как реальный браузер).

        Порядок: в самый хвост body. Шлём ОБА только если fb_dtsg надёжно извлёкся
        (браузер никогда не шлёт configure без обоих; неверный токен ломает залив,
        поэтому при неудаче извлечения — ни одного, текущее рабочее поведение).
        jazoest = "2" + sum(ord) от csrftoken (сверено с HAR).
        """
        dtsg = self._fetch_fb_dtsg()
        if not dtsg:
            return
        csrf = self.cookies.get("csrftoken", "")
        data["jazoest"] = "2" + str(sum(ord(c) for c in csrf))
        data["fb_dtsg"] = dtsg

    def close(self):
        self._client.close()

    def current_cookies(self) -> dict:
        """Актуальные куки: исходные + server-set (Instagram ротирует sessionid/csrftoken во время работы)."""
        merged = dict(self.cookies)
        try:
            for c in self._client.cookies.jar:
                if c.name and c.value:
                    merged[c.name] = c.value
        except Exception:
            pass
        return merged

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ==================== HEADERS ====================

    def _base_headers(self, url: str = "https://www.instagram.com/",
                      method: str = "GET") -> dict:
        """Заголовки XHR под реальный Chrome (сверено побайтно с HAR).

        - i.instagram.com (rupload): УРЕЗАННЫЙ набор — только low-entropy hints
          (sec-ch-ua/-mobile/-platform), БЕЗ full-version-list/platform-version/
          model/prefers-color-scheme и БЕЗ x-csrftoken/x-ig-www-claim/x-requested-with.
        - www.instagram.com: полный набор.
        - x-instagram-ajax — ТОЛЬКО на POST (у GET-ов его нет).
        - origin — на POST и на cross-origin (i.instagram.com); НЕ на same-origin GET.
        - sec-fetch-site: i.instagram.com=same-site, www=same-origin.
        Навигационные upgrade-insecure-requests/sec-fetch-user срезает транспорт.
        """
        is_i_host = "i.instagram.com" in url
        is_write = (method or "GET").upper() not in ("GET", "HEAD")
        h = {
            "accept": "*/*",
            "accept-language": self.accept_language,
            "referer": "https://www.instagram.com/",
            "user-agent": self.user_agent,
            "sec-ch-ua": self.sec_ch_ua,
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
            "x-asbd-id": ASBD_ID,
            "x-ig-app-id": APP_ID,
            "x-ig-max-touch-points": "0",
            "x-web-session-id": self.web_session_id,
            "sec-fetch-site": "same-site" if is_i_host else "same-origin",
            "sec-fetch-mode": "cors",
            "sec-fetch-dest": "empty",
            "priority": "u=1, i",
        }
        if is_write:
            h["x-instagram-ajax"] = _ajax_version  # только на POST
        if not is_i_host:
            # www — полный набор high-entropy hints + csrf/claim/xrw
            h["sec-ch-ua-full-version-list"] = self.sec_ch_ua_full
            h["sec-ch-ua-platform-version"] = self.platform_version
            h["sec-ch-ua-model"] = '""'
            h["sec-ch-prefers-color-scheme"] = "light"
            h["x-csrftoken"] = self.cookies.get("csrftoken", "")
            h["x-ig-www-claim"] = self.www_claim
            h["x-requested-with"] = "XMLHttpRequest"
        if is_write or is_i_host:
            h["origin"] = "https://www.instagram.com"
        return h

    # ==================== SESSION VALIDATION ====================

    def session_status(self) -> str:
        """Состояние сессии: 'ok' | 'login_required' | 'challenge' | 'error'.

        ВАЖНО: зачелленженная сессия отдаёт HTTP 200 с телом
        {"message":"checkpoint_required"/"feedback_required",...} — поэтому 200
        НЕ означает 'ok'. Тело инспектируем через classify_ig_block.
        """
        if not self.cookies.get("sessionid") or not self.cookies.get("ds_user_id"):
            return "login_required"

        def _probe(url):
            r = self._client.get(url, headers=self._base_headers(url, "GET"),
                                 cookies=self.cookies, timeout=15)
            cls = classify_ig_block(r.status_code, r.text)
            if cls == "login_required":
                return "login_required"
            if cls in ("challenge", "spam", "rate_limited"):
                return "challenge"
            if r.status_code == 200:
                # Валидная сессия → API отдаёт JSON. HTML-200 = login-страница
                # (мёртвая/разлогиненная сессия редиректит на неё) → не 'ok'.
                if r.text.lstrip()[:1] in ("{", "["):
                    return "ok"
                return "login_required"
            return ""  # неизвестный non-200 → пробуем fallback

        try:
            st = _probe("https://www.instagram.com/api/v1/feed/reels_tray/")
            if st:
                return st
            st = _probe("https://www.instagram.com/api/v1/users/web_profile_info/?username=instagram")
            return st or "error"
        except Exception as e:
            logger.debug(f"auth probe error: {e}")
            return "error"

    def is_authenticated(self) -> bool:
        """Совместимость: True только если сессия полностью валидна (не challenge)."""
        return self.session_status() == "ok"

    # ==================== UPLOAD ====================

    def upload_reel(self, video_path: Path | str, caption: str = "") -> dict:
        """Upload a single reel. Returns {'ok': bool, 'code': str, 'pk': str, ...}."""
        video_path = Path(video_path)
        if not video_path.exists():
            raise WebUploadError(f"Video not found: {video_path}")

        width, height, dur_ms = _ffprobe_dims(video_path)
        # upload_id = чистый timestamp мс (13 знаков) — как реальный web (сверено HAR).
        upload_id = str(int(time.time() * 1000))
        logger.debug(f"upload_reel: {video_path.name} {width}x{height} {dur_ms}ms id={upload_id}")

        video_bytes = video_path.read_bytes()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
            cover_path = Path(tf.name)
        try:
            _extract_cover(video_path, cover_path)
            cover_bytes = cover_path.read_bytes()
        finally:
            cover_path.unlink(missing_ok=True)

        # 1. Reserve upload slot (GET) — БЕЗ rupload-params (как браузер по HAR:
        #    GET-проба несёт только базовые заголовки, без params и x-instagram-ajax).
        url_v = f"https://i.instagram.com/rupload_igvideo/fb_uploader_{upload_id}"
        # params_v — порядок и набор полей побайтно как в HAR (11 полей, без
        # video_edit_params/retry_context/xsharing_user_ids — браузер их не шлёт).
        params_v = {
            "client-passthrough": "1",
            "is_clips_video": "1",
            "for_album": False,
            "is_sidecar": "0",
            "media_type": 2,
            "upload_id": upload_id,
            "upload_media_duration_ms": dur_ms,
            "upload_media_height": height,
            "upload_media_width": width,
            "video_format": "",
            "video_transform": None,
        }
        rupload_hdr = json.dumps(params_v, separators=(",", ":"))
        r = self._client.get(url_v, headers=self._base_headers(url_v, "GET"),
                             cookies=self.cookies)
        if r.status_code >= 400:
            _raise_upload_error("probe", r)

        # 2. Upload video bytes — с rupload-params + entity-заголовки. Видео-POST
        #    по HAR БЕЗ content-type и БЕЗ x-entity-type (браузер шлёт Blob без type).
        hdrs_v = {
            **self._base_headers(url_v, "POST"),
            "x-instagram-rupload-params": rupload_hdr,
            "offset": "0",
            "x-entity-name": f"fb_uploader_{upload_id}",
            "x-entity-length": str(len(video_bytes)),
        }
        r = self._client.post(url_v, headers=hdrs_v, cookies=self.cookies, content=video_bytes)
        if r.status_code >= 400:
            _raise_upload_error("video upload", r)
        try:
            j = r.json()
            if j.get("status") != "ok":
                cls = classify_ig_block(200, r.text, j)
                if cls in ("challenge", "spam", "rate_limited"):
                    raise WebChallengeError(f"video upload challenge: {cls}")
                raise WebUploadError(f"video upload status not ok: {r.text[:200]}")
        except ValueError:
            raise WebUploadError(f"video upload: bad JSON: {r.text[:200]}")

        # 3. Upload cover (as photo)
        url_p = f"https://i.instagram.com/rupload_igphoto/fb_uploader_{upload_id}"
        params_p = {"media_type": 2, "upload_id": upload_id,
                    "upload_media_height": height, "upload_media_width": width}
        hdrs_p = {
            **self._base_headers(url_p, "POST"),
            "x-instagram-rupload-params": json.dumps(params_p, separators=(",", ":")),
            "offset": "0",
            "content-type": "image/jpeg",
            "x-entity-name": f"fb_uploader_{upload_id}",
            "x-entity-length": str(len(cover_bytes)),
            "x-entity-type": "image/jpeg",
        }
        r = self._client.post(url_p, headers=hdrs_p, cookies=self.cookies, content=cover_bytes)
        if r.status_code >= 400:
            _raise_upload_error("cover upload", r)

        # 4. Configure to clips (publish) — повторять пока status=202 "transcode not finished"
        url_c = "https://www.instagram.com/api/v1/media/configure_to_clips/"
        hdrs_c = {
            **self._base_headers(url_c, "POST"),
            "content-type": "application/x-www-form-urlencoded",
        }
        data = {
            "archive_only": "false",
            "caption": caption or "",
            "clips_share_preview_to_feed": "1",
            "disable_comments": "0",
            "disable_oa_reuse": "false",
            "igtv_share_preview_to_feed": "1",
            "is_meta_only_post": "0",
            "is_unified_video": "1",
            "like_and_view_counts_disabled": "0",
            "media_share_flow": "creation_flow",
            "share_to_facebook": "",
            "share_to_fb_destination_type": "USER",
            "source_type": "library",
            "upload_id": upload_id,
            "video_subtitles_enabled": "0",
        }
        self._apply_dtsg(data)
        last_body = ""
        for attempt in range(8):
            r = self._client.post(url_c, headers=hdrs_c, cookies=self.cookies, data=data)
            last_body = r.text[:300]
            # 401/403 -> сессия протухла
            if r.status_code in (401, 403):
                raise WebUploadError(f"session invalid during configure ({r.status_code})")
            if r.status_code == 200:
                try:
                    j = r.json()
                except ValueError:
                    j = {}
                if j.get("status") == "ok":
                    media = j.get("media", {}) or {}
                    return {
                        "ok": True,
                        "code": media.get("code"),
                        "pk": media.get("pk") or media.get("id", "").split("_")[0],
                        "media_id": media.get("id"),
                        "taken_at": media.get("taken_at"),
                    }
                # "transcode not finished" может вернуться и с 200
                if j.get("status") == "processing" or "Transcode not finished" in last_body:
                    time.sleep(_transcode_sleep())
                    continue
                # 200, но не ok и не processing → терминальный отказ:
                # классифицируем и сразу бросаем (не долбим оставшиеся попытки).
                cls = classify_ig_block(200, r.text, j)
                if cls in ("challenge", "spam", "rate_limited"):
                    raise WebChallengeError(f"configure challenge: {cls}: {last_body}")
                if cls == "login_required":
                    raise WebUploadError("session invalid during configure (200 login_required)")
                raise WebUploadError(f"configure rejected (200): {last_body}")
            if r.status_code == 202:
                time.sleep(_transcode_sleep())
                continue
            if r.status_code >= 400:
                _raise_upload_error("configure", r)
        raise WebUploadError(f"configure exhausted retries: {last_body}")

    # ==================== STORY UPLOAD ====================

    def upload_story(self, media_path: Path | str, *,
                     besties: bool = False, link: dict | None = None) -> dict:
        """Опубликовать сторис из фото или видео.

        link: {"url": str, "x"?, "y"?, "width"?, "height"?, "rotation"?} — link-стикер.
        Возвращает {'ok': bool, 'code': str|None, 'pk': str|None, ...}.
        """
        media_path = Path(media_path)
        if not media_path.exists():
            raise WebUploadError(f"Media not found: {media_path}")

        upload_id = str(int(time.time() * 1000))
        ext = media_path.suffix.lower()
        if ext in (".mp4", ".mov", ".avi", ".mkv", ".webm"):
            return self._upload_story_video(media_path, upload_id, besties=besties, link=link)
        return self._upload_story_photo(media_path, upload_id, besties=besties, link=link)

    def _prepare_link_sticker(self, link: dict | None):
        """Отрисовать link-стикер и подогнать бокс тап-зоны под реальный визуал.

        Текст = label (или домен) в верхнем регистре — как в my_story_ext.
        Возвращает (sticker|None, link). Если рендер недоступен — (None, link):
        уйдёт только тап-зона (визуала не будет, но залив не сломается).
        """
        if not link or not link.get("url"):
            return None, link
        from urllib.parse import urlparse
        host = (urlparse(link["url"]).hostname or "").replace("www.", "")
        text = (link.get("label") or host).upper()
        r = render_link_sticker(text, scale=float(link.get("scale", 1.0) or 1.0),
                                variant=link.get("variant", "default"))
        if not r:
            return None, link
        sticker, w_norm, h_norm = r
        # бокс тап-зоны = реально отрисованный стикер (иначе клик не совпадёт)
        return sticker, {**link, "width": w_norm, "height": h_norm}

    def _upload_story_photo(self, image_path: Path, upload_id: str, *,
                            besties: bool, link: dict | None) -> dict:
        jpeg_bytes, w, h = _story_photo_bytes(image_path)
        sticker, link = self._prepare_link_sticker(link)
        if sticker is not None:
            jpeg_bytes = _bake_link_on_photo(jpeg_bytes, sticker,
                                             link.get("x", 0.5), link.get("y", 0.82))
        self._rupload_story_photo(upload_id, jpeg_bytes, w, h, is_video_cover=False)
        return self._configure_to_story(upload_id, besties=besties, link=link)

    def _upload_story_video(self, video_path: Path, upload_id: str, *,
                            besties: bool, link: dict | None) -> dict:
        # Link-стикер: вшиваем в кадры видео (ffmpeg overlay), иначе ссылка
        # будет кликабельной, но НЕвидимой.
        sticker, link = self._prepare_link_sticker(link)
        baked = None
        if sticker is not None:
            baked = _bake_link_on_video(video_path, sticker,
                                        link.get("x", 0.5), link.get("y", 0.82))
            if baked:
                video_path = baked

        try:
            width, height, _dur_ms = _ffprobe_dims(video_path)
            video_bytes = video_path.read_bytes()

            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tf:
                cover_path = Path(tf.name)
            try:
                _extract_cover(video_path, cover_path)
                cover_bytes = cover_path.read_bytes()
            finally:
                cover_path.unlink(missing_ok=True)

            # Порядок как в расширении: сначала видео, затем обложка тем же upload_id.
            self._rupload_story_video(upload_id, video_bytes)
            self._rupload_story_photo(upload_id, cover_bytes, width, height,
                                      is_video_cover=True)
            return self._configure_to_story(upload_id, besties=besties, link=link)
        finally:
            if baked:
                Path(baked).unlink(missing_ok=True)

    def _rupload_story_photo(self, upload_id: str, jpeg_bytes: bytes,
                             width: int, height: int, *, is_video_cover: bool) -> None:
        import random
        name = f"{upload_id}_0_{random.randint(0, 999_999_999)}"
        url = f"https://i.instagram.com/rupload_igphoto/{name}"
        # По reel-HAR (тот же rupload_igphoto): фото-POST несёт content-type +
        # x-entity-type image/jpeg; без retry_context/xsharing.
        params = {
            "upload_id": upload_id,
            "media_type": 2 if is_video_cover else 1,
            "upload_media_width": width,
            "upload_media_height": height,
        }
        hdrs = {
            **self._base_headers(url, "POST"),
            "offset": "0",
            "content-type": "image/jpeg",
            "x-entity-name": name,
            "x-entity-length": str(len(jpeg_bytes)),
            "x-entity-type": "image/jpeg",
            "x-instagram-rupload-params": json.dumps(params, separators=(",", ":")),
        }
        r = self._client.post(url, headers=hdrs, cookies=self.cookies, content=jpeg_bytes)
        if r.status_code >= 400:
            _raise_upload_error("story photo upload", r)

    def _rupload_story_video(self, upload_id: str, video_bytes: bytes) -> None:
        import random
        name = f"{upload_id}_0_{random.randint(0, 999_999_999)}"
        url = f"https://i.instagram.com/rupload_igvideo/{name}"
        params = {
            "client-passthrough": "1",
            "is_sidecar": "0",
            "media_type": 2,
            "upload_id": upload_id,
            "for_album": True,
            "is_unified_video": "0",
        }
        # По reel-HAR (тот же rupload_igvideo): видео-POST БЕЗ x-entity-type
        # и без retry_context/xsharing (content-type curl_cffi добавит сам).
        hdrs = {
            **self._base_headers(url, "POST"),
            "offset": "0",
            "x-entity-name": name,
            "x-entity-length": str(len(video_bytes)),
            "x-instagram-rupload-params": json.dumps(params, separators=(",", ":")),
        }
        r = self._client.post(url, headers=hdrs, cookies=self.cookies, content=video_bytes)
        if r.status_code >= 400:
            _raise_upload_error("story video upload", r)

    def _configure_to_story(self, upload_id: str, *,
                            besties: bool = False, link: dict | None = None) -> dict:
        url = "https://www.instagram.com/api/v1/web/create/configure_to_story/"
        data = {"upload_id": upload_id}
        if besties:
            data["audience"] = "besties"
        if link and link.get("url"):
            sticker = {
                "x": link.get("x", 0.5),
                "y": link.get("y", 0.82),
                "width": link.get("width", 0.42),
                "height": link.get("height", 0.075),
                "rotation": link.get("rotation", 0.0),
                "link_type": "web",
                "url": link["url"],
            }
            data["story_link_stickers"] = json.dumps([sticker], separators=(",", ":"))
        self._apply_dtsg(data)
        hdrs = {
            **self._base_headers(url, "POST"),
            "content-type": "application/x-www-form-urlencoded",
        }
        last_body = ""
        for _attempt in range(8):
            r = self._client.post(url, headers=hdrs, cookies=self.cookies, data=data)
            last_body = r.text[:300]
            if r.status_code in (401, 403):
                raise WebUploadError(f"session invalid during configure_to_story ({r.status_code})")
            if r.status_code == 200:
                if "Transcode not finished" in r.text:
                    time.sleep(_transcode_sleep())
                    continue
                try:
                    j = r.json()
                except ValueError:
                    j = {}
                if j.get("status") == "ok":
                    media = j.get("media", {}) or {}
                    return {
                        "ok": True,
                        "code": media.get("code"),
                        "pk": media.get("pk") or (media.get("id", "") or "").split("_")[0],
                        "media_id": media.get("id"),
                    }
                cls = classify_ig_block(200, r.text, j)
                if cls in ("challenge", "spam", "rate_limited"):
                    raise WebChallengeError(f"configure_to_story challenge: {cls}: {last_body}")
                if cls == "login_required":
                    raise WebUploadError("session invalid during configure_to_story (200 login_required)")
                raise WebUploadError(f"configure_to_story rejected: {last_body}")
            if r.status_code == 202:
                time.sleep(_transcode_sleep())
                continue
            if r.status_code >= 400:
                _raise_upload_error("configure_to_story", r)
        raise WebUploadError(f"configure_to_story exhausted retries: {last_body}")


# ==================== LOGIN VIA PLAYWRIGHT ====================

async def web_login_playwright(
    username: str,
    password: str,
    otp_secret: str = "",
    proxy: str | None = None,
    headless: bool = True,
    log_cb=None,
    ua_info: dict | None = None,
) -> dict:
    """Perform login via Playwright, return cookie dict + metadata.

    ua_info: если передан — используем его UA; иначе выбираем по username из ua_pool.
    Returns {
        'ok': bool,
        'cookies': {name: value, ...},
        'cookies_full': [...],
        'reason': str,
        'www_claim': str,
        'user_agent': str,
        'sec_ch_ua': str,
        'sec_ch_ua_full': str,
        'platform_version': str,
    }
    """
    from playwright.async_api import async_playwright
    from .ua_pool import pick_ua_for_user

    def log(msg):
        if log_cb:
            log_cb(msg)
        else:
            logger.info(msg)

    # Выбираем UA для этого username (стабильный hash-based)
    ua = ua_info or pick_ua_for_user(username)
    log(f"  🪪 UA: {ua['ua'].split('Chrome/')[1].split(' ')[0]} ({ua['browser']}) on {ua['platform_version']}")

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": headless,
            "args": ["--disable-blink-features=AutomationControlled",
                     "--window-size=1280,900"],
        }
        context_kwargs = {
            "user_agent": ua["ua"],
            "viewport": {"width": 1280, "height": 900},
            "locale": "en-US",
        }
        if proxy:
            launch_kwargs["proxy"] = _parse_proxy(proxy)

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(**context_kwargs)
        await context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            "Object.defineProperty(navigator, 'languages', {get: () => ['en-US','en']});"
            "Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});"
        )
        page = await context.new_page()

        www_claim = DEFAULT_WWW_CLAIM

        # Capture x-ig-www-claim from any response header
        def on_resp(resp):
            nonlocal www_claim
            try:
                v = resp.headers.get("x-ig-set-www-claim") or resp.headers.get("ig-set-x-ig-www-claim")
                if v:
                    www_claim = v
            except Exception:
                pass

        page.on("response", on_resp)

        try:
            await page.goto("https://www.instagram.com/accounts/login/",
                            wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(4500)

            # Cookie banner
            for lbl in ["Allow all cookies", "Accept all", "Only allow essential cookies"]:
                try:
                    btn = await page.query_selector(f'button:has-text("{lbl}")')
                    if btn:
                        await btn.click()
                        await page.wait_for_timeout(1500)
                        break
                except Exception:
                    pass

            # Username
            user_sel = None
            for sel in ['input[name="username"]', 'input[aria-label*="hone"]',
                        'input[aria-label*="mail"]', 'input[type="text"]']:
                try:
                    await page.wait_for_selector(sel, timeout=4000)
                    await page.fill(sel, username)
                    user_sel = sel
                    break
                except Exception:
                    continue
            if not user_sel:
                await browser.close()
                return {"ok": False, "reason": "username_field_not_found"}

            # Password
            for sel in ['input[name="password"]', 'input[type="password"]']:
                try:
                    await page.wait_for_selector(sel, timeout=3000)
                    await page.fill(sel, password)
                    break
                except Exception:
                    continue
            await page.wait_for_timeout(1000)

            # Submit
            clicked = False
            for sel in ['button[type="submit"]', 'button:has-text("Log in")',
                        'div[role="button"]:has-text("Log in")']:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        clicked = True
                        break
                except Exception:
                    continue
            if not clicked:
                await page.keyboard.press("Enter")

            await page.wait_for_timeout(9000)

            # 2FA если нужно
            html = (await page.content()).lower()
            needs_2fa = any(m in html for m in ["verificationcode", "authenticator", "two_factor",
                                                "6-digit code", "6-значн"])
            if not needs_2fa:
                try:
                    needs_2fa = bool(await page.query_selector('input[name="verificationCode"]'))
                except Exception:
                    pass

            if needs_2fa:
                if not otp_secret:
                    await browser.close()
                    return {"ok": False, "reason": "2fa_required_no_secret"}
                code = totp(otp_secret)
                log(f"  2FA TOTP: {code}")
                filled = False
                for sel in ['input[name="verificationCode"]',
                            'input[aria-label*="ode"]',
                            'input[autocomplete="one-time-code"]',
                            'input[type="tel"]']:
                    try:
                        await page.wait_for_selector(sel, timeout=3000)
                        await page.fill(sel, code)
                        filled = True
                        break
                    except Exception:
                        continue
                if not filled:
                    await browser.close()
                    return {"ok": False, "reason": "2fa_field_not_found"}
                await page.wait_for_timeout(800)
                for sel in ['button:has-text("Confirm")',
                            'button:has-text("Continue")',
                            'button[type="submit"]']:
                    try:
                        el = await page.query_selector(sel)
                        if el:
                            await el.click()
                            break
                    except Exception:
                        continue
                else:
                    await page.keyboard.press("Enter")
                await page.wait_for_timeout(9000)

            # "Save info?" + notifications popups
            for lbl in ["Not now", "Not Now", "Save Info", "Skip", "Later"]:
                try:
                    b = await page.query_selector(f'button:has-text("{lbl}")')
                    if b:
                        await b.click()
                        await page.wait_for_timeout(2500)
                except Exception:
                    pass

            # Verify sessionid
            cookies_full = await context.cookies()
            sess = next((c for c in cookies_full if c["name"] == "sessionid"), None)
            if not sess:
                await browser.close()
                # Detect suspension/captcha
                body = await page.content()
                reason = "no_session"
                for marker, r in [("has been disabled", "account_disabled"),
                                  ("suspicious", "challenge_required"),
                                  ("challenge", "challenge_required"),
                                  ("Please wait", "rate_limited"),
                                  ("password you entered is incorrect", "bad_password")]:
                    if marker.lower() in body.lower():
                        reason = r
                        break
                return {"ok": False, "reason": reason}

            cookies_dict = {c["name"]: c["value"] for c in cookies_full}
            await browser.close()
            return {
                "ok": True,
                "cookies": cookies_dict,
                "cookies_full": cookies_full,
                "www_claim": www_claim,
                "user_agent": ua["ua"],
                "sec_ch_ua": ua["sec_ch_ua"],
                "sec_ch_ua_full": ua["sec_ch_ua_full"],
                "platform_version": ua["platform_version"],
                "browser": ua["browser"],
            }
        except Exception as e:
            try:
                await browser.close()
            except Exception:
                pass
            return {"ok": False, "reason": f"exception: {e}"}


def rotate_proxy_ip(proxy_info: dict, *, wait_sec: float = 12.0,
                    log=None, stop_check=None) -> dict:
    """Смена IP через change_ip_url → пауза → проверка текущего IP через прокси.

    proxy_info: {"url": "http://...", "change_ip_url": "https://...", "key": "proxy"}
    wait_sec: задержка после смены IP (10-15с для восстановления сети)

    Returns: {"ok": bool, "ip": "x.x.x.x" | None, "error": str}
    """
    def _log(msg):
        if log:
            try: log(msg)
            except Exception: pass
        else:
            logger.info(msg)

    url = proxy_info.get("url")
    change_url = proxy_info.get("change_ip_url")
    key = proxy_info.get("key", "proxy")

    if not change_url:
        _log(f"  ⚠️ {key}: change_ip_url не задан — пропускаю смену IP")
        return {"ok": False, "ip": None, "error": "no_change_url"}

    # 1. Дёрнуть смену IP (запрос БЕЗ прокси, напрямую к провайдеру)
    _log(f"  🔄 {key}: меняю IP через change_ip_url...")
    try:
        r = httpx.get(change_url, timeout=15, follow_redirects=True)
        body = (r.text or "")[:120].strip().replace("\n", " ")
        _log(f"  🔄 {key}: change_ip → HTTP {r.status_code} {body}")
    except Exception as e:
        _log(f"  ❌ {key}: change_ip error: {e}")
        return {"ok": False, "ip": None, "error": f"change_failed: {e}"}

    # 2. Ждать восстановления сети (прерываемо через stop_check)
    _log(f"  ⏳ {key}: жду {int(wait_sec)}с восстановления сети...")
    end = time.time() + wait_sec
    while time.time() < end:
        if stop_check and stop_check():
            return {"ok": False, "ip": None, "error": "stopped"}
        time.sleep(min(1.0, end - time.time()))

    # 3. Проверить текущий IP через прокси
    ip = None
    err = ""
    for probe_url in ("https://api.ipify.org?format=json",
                      "https://ifconfig.me/ip",
                      "https://ipinfo.io/ip"):
        try:
            with httpx.Client(proxy=url, timeout=15, follow_redirects=True) as c:
                resp = c.get(probe_url)
            if resp.status_code == 200:
                text = resp.text.strip()
                # ipify вернёт JSON
                if text.startswith("{"):
                    try:
                        ip = (resp.json().get("ip") or "").strip()
                    except Exception:
                        ip = None
                else:
                    ip = text.split()[0] if text else None
                if ip and len(ip) < 60:
                    break
        except Exception as e:
            err = str(e)
            continue
    if ip:
        _log(f"  🌐 {key}: новый IP → {ip}")
        return {"ok": True, "ip": ip, "error": ""}
    _log(f"  ⚠️ {key}: не удалось получить IP ({err})")
    return {"ok": False, "ip": None, "error": err or "no_ip"}


def _parse_proxy(proxy_str: str) -> dict:
    """Parse 'http://user:pass@host:port' or 'host:port' to Playwright format."""
    if not proxy_str:
        return {}
    if "://" not in proxy_str:
        proxy_str = "http://" + proxy_str
    from urllib.parse import urlparse
    u = urlparse(proxy_str)
    out = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username:
        out["username"] = u.username
    if u.password:
        out["password"] = u.password
    return out
