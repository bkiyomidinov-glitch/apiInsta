"""Прямой API-логин Instagram через graphql `useCDSWebLoginMutation`.

Без Playwright — чистый HTTP:
1. GET /accounts/login/ → парсим sharedData (csrftoken, lsd, encryption.public_key, key_id, version)
2. Шифруем пароль: Curve25519 SealedBox + AES-GCM → "#PWD_BROWSER:<ver>:<time>:<b64>"
3. POST /api/graphql (doc_id=9807605492696448) с encrypted password + identifier
4. Если `two_factor_required` → POST /api/v1/web/accounts/login/ajax/two_factor/ с TOTP-кодом
5. Куки из httpx cookies jar (Set-Cookie)

Совместимо по headers с IgWebClient (x-ig-app-id, x-fb-lsd, x-csrftoken, sec-ch-ua пул).
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import time
from typing import Optional

import httpx
from nacl.public import PublicKey, SealedBox
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .ua_pool import pick_ua_for_user

logger = logging.getLogger(__name__)

APP_ID = "936619743392459"
ASBD_ID = "359341"
AJAX_VERSION = "1037644188"
LOGIN_DOC_ID = "9807605492696448"  # useCDSWebLoginMutation


def totp(secret: str) -> str:
    secret = (secret or "").replace(" ", "").upper()
    padding = "=" * ((8 - len(secret) % 8) % 8)
    key = base64.b32decode(secret + padding)
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[19] & 0x0F
    code = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{code % 1_000_000:06d}"


def _encrypt_password(password: str, encryption: dict) -> str:
    """Шифрование пароля по схеме Instagram web (sealed box + AES-GCM)."""
    public_key_hex = encryption["public_key"]
    key_id = int(encryption["key_id"])
    version = int(encryption["version"])

    pub_bytes = bytes.fromhex(public_key_hex)
    session_key = os.urandom(32)
    iv = b"\x00" * 12
    current_time = int(time.time())

    # Wrap session key via Curve25519 sealed box
    sbox = SealedBox(PublicKey(pub_bytes))
    enc_session_key = sbox.encrypt(session_key)  # 48 + 32 = 80 bytes

    # AES-GCM: encrypt password with time as AAD
    aesgcm = AESGCM(session_key)
    ct_full = aesgcm.encrypt(iv, password.encode("utf-8"), str(current_time).encode("ascii"))
    ciphertext = ct_full[:-16]
    tag = ct_full[-16:]

    # Payload: 0x01 | key_id | rsa_size_le | sealed_box | tag | ciphertext
    payload = (
        b"\x01" +
        bytes([key_id]) +
        struct.pack("<H", len(enc_session_key)) +
        enc_session_key +
        tag +
        ciphertext
    )
    return f"#PWD_BROWSER:{version}:{current_time}:{base64.b64encode(payload).decode()}"


def _parse_shared_data(html: str) -> dict:
    """Достаёт sharedData (sprite + config) с login-страницы."""
    # 1. Пытаемся найти блок ScheduledServerJS с "config":{...,"csrf_token":"..."}
    # 2. encryption: {"public_key":"...","key_id":"...","version":"..."}
    out = {}

    # csrf token
    m = re.search(r'\\?"csrf_token\\?"\s*:\s*\\?"([a-zA-Z0-9_\-]+)\\?"', html)
    if m:
        out["csrf_token"] = m.group(1)

    # lsd
    m = re.search(r'\\?"LSD\\?".*?\\?"token\\?"\s*:\s*\\?"([a-zA-Z0-9_\-:]+)\\?"', html)
    if m:
        out["lsd"] = m.group(1)
    else:
        m = re.search(r'name="lsd"\s+value="([^"]+)"', html)
        if m:
            out["lsd"] = m.group(1)

    # encryption block: "InstagramPasswordEncryption",[],{"key_id":"N","public_key":"hex","version":"N"}
    # Поля могут идти в любом порядке — парсим по отдельности внутри блока
    m = re.search(r'InstagramPasswordEncryption\"\s*,\s*\[\],\s*\{([^}]+)\}', html)
    if m:
        block = m.group(1)
        pk = re.search(r'\"public_key\"\s*:\s*\"([0-9a-f]+)\"', block)
        kid = re.search(r'\"key_id\"\s*:\s*\"(\d+)\"', block)
        ver = re.search(r'\"version\"\s*:\s*\"(\d+)\"', block)
        if pk and kid and ver:
            out["encryption"] = {
                "public_key": pk.group(1),
                "key_id": kid.group(1),
                "version": ver.group(1),
            }

    # jazoest — можно не парсить, всегда можно посчитать
    if "lsd" in out:
        out["jazoest"] = _compute_jazoest(out["lsd"])

    # Device id (часто в cookies как ig_did) — генерим uuid если надо
    m = re.search(r'\\?"device_id\\?"\s*:\s*\\?"([A-F0-9\-]+)\\?"', html)
    if m:
        out["device_id"] = m.group(1)

    # rollout_hash может понадобиться для x-instagram-ajax / __rev / __spin_r
    m = re.search(r'\"rollout_hash\"\s*:\s*\"(\d+)\"', html)
    if m:
        out["rollout_hash"] = m.group(1)

    # hsi — Relay session id
    m = re.search(r'\"hsi\"\s*:\s*\"(\d+)\"', html)
    if m:
        out["hsi"] = m.group(1)

    # __hs — hash seed
    m = re.search(r'\"haste_session\"\s*:\s*\"([^\"]+)\"', html)
    if m:
        out["haste_session"] = m.group(1)

    return out


def _compute_jazoest(lsd: str) -> str:
    """Jazoest = '2' + sum(ord(c) for c in lsd)."""
    return "2" + str(sum(ord(c) for c in lsd))


def _build_headers(ua_info: dict, csrf: str, lsd: str, ajax: str = AJAX_VERSION) -> dict:
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://www.instagram.com",
        "referer": "https://www.instagram.com/accounts/login/",
        "user-agent": ua_info["ua"],
        "sec-ch-ua": ua_info["sec_ch_ua"],
        "sec-ch-ua-full-version-list": ua_info["sec_ch_ua_full"],
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-model": '""',
        "sec-ch-ua-platform": '"macOS"',
        "sec-ch-ua-platform-version": ua_info["platform_version"],
        "sec-ch-prefers-color-scheme": "light",
        "x-ig-app-id": APP_ID,
        "x-ig-www-claim": "0",
        "x-asbd-id": ASBD_ID,
        "x-instagram-ajax": ajax,
        "x-csrftoken": csrf,
        "x-fb-lsd": lsd,
        "x-fb-friendly-name": "useCDSWebLoginMutation",
        "x-requested-with": "XMLHttpRequest",
        "content-type": "application/x-www-form-urlencoded",
    }


def api_login(username: str, password: str, otp_secret: str = "", *,
              ua_info: Optional[dict] = None,
              proxy: Optional[str] = None,
              log=None,
              timeout: float = 30.0) -> dict:
    """Логин чисто через API. Возвращает тот же формат что web_login_playwright():
    {
        'ok': bool,
        'cookies': {name: value},
        'user_agent', 'sec_ch_ua', 'sec_ch_ua_full', 'platform_version', 'browser',
        'www_claim': '',
        'reason': str  # если ok=False
    }
    """
    def _log(msg):
        if log:
            try: log(msg)
            except: pass
        else:
            logger.info(msg)

    ua = ua_info or pick_ua_for_user(username)
    _log(f"  🪪 UA: Chrome {ua['ua'].split('Chrome/')[1].split(' ')[0]} / {ua['platform_version']}")

    client_kwargs = {"timeout": timeout, "follow_redirects": True}
    if proxy:
        try:
            client_kwargs["proxy"] = proxy
        except Exception:
            client_kwargs["proxies"] = proxy

    with httpx.Client(**client_kwargs) as client:
        # Initial headers — для первичного GET
        client.headers.update({
            "user-agent": ua["ua"],
            "accept-language": "en-US,en;q=0.9",
            "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "sec-ch-ua": ua["sec_ch_ua"],
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"macOS"',
        })

        # 1. Загрузить login-страницу и извлечь sharedData
        try:
            r = client.get("https://www.instagram.com/accounts/login/")
        except Exception as e:
            return {"ok": False, "reason": f"fetch_login_page: {e}"}
        if r.status_code != 200:
            return {"ok": False, "reason": f"login_page_status_{r.status_code}"}

        shared = _parse_shared_data(r.text)
        csrf = shared.get("csrf_token") or client.cookies.get("csrftoken") or ""
        lsd = shared.get("lsd") or ""
        enc = shared.get("encryption")
        if not csrf or not lsd or not enc:
            return {"ok": False,
                    "reason": f"shared_data_incomplete: csrf={bool(csrf)} lsd={bool(lsd)} enc={bool(enc)}"}

        enc_password = _encrypt_password(password, enc)
        # Для REST эндпоинта используем префикс #PWD_INSTAGRAM_BROWSER
        enc_password_rest = enc_password.replace("#PWD_BROWSER:", "#PWD_INSTAGRAM_BROWSER:")
        jazoest = shared.get("jazoest") or _compute_jazoest(lsd)
        device_id = client.cookies.get("ig_did") or shared.get("device_id") or ""
        rollout = shared.get("rollout_hash") or AJAX_VERSION

        # 2. REST Login endpoint /api/v1/web/accounts/login/ajax/ — более стабильный чем graphql
        headers = _build_headers(ua, csrf, lsd, ajax=rollout)
        headers.pop("x-fb-friendly-name", None)
        rest_body = {
            "enc_password": enc_password_rest,
            "caaF2DebugGroup": "0",
            "loginAttemptSubmissionCount": "0",
            "username": username,
            "queryParams": "{}",
            "optIntoOneTap": "false",
            "trustedDeviceRecords": "{}",
            "jazoest": jazoest,
        }
        try:
            r = client.post(
                "https://www.instagram.com/api/v1/web/accounts/login/ajax/",
                headers=headers, data=rest_body)
        except Exception as e:
            return {"ok": False, "reason": f"login_request: {e}"}
        if r.status_code >= 500:
            return {"ok": False, "reason": f"login_status_{r.status_code}: {r.text[:200]}"}
        try:
            jr = r.json()
        except Exception:
            return {"ok": False, "reason": f"login_bad_json ({r.status_code}): {r.text[:300]}"}

        if jr.get("authenticated") and jr.get("status") == "ok":
            _log(f"  ✅ Авторизован (без 2FA)")
            return _build_success(client, ua)

        if jr.get("two_factor_required") or jr.get("message") == "two_factor_required":
            info = jr.get("two_factor_info") or {}
            identifier = info.get("two_factor_identifier") or ""
            if not identifier:
                return {"ok": False, "reason": "2fa_missing_identifier"}
            if not otp_secret:
                return {"ok": False, "reason": "2fa_required_no_secret"}
            code = totp(otp_secret)
            _log(f"  🔐 2FA → код {code}")
            r2 = _post_two_factor(client, ua, csrf, lsd, identifier, code, username, jazoest=jazoest)
            if not r2["ok"]:
                return r2
            _log(f"  ✅ 2FA принят")
            return _build_success(client, ua)

        if jr.get("checkpoint_url"):
            return {"ok": False, "reason": f"checkpoint_required: {jr.get('checkpoint_url','')[:100]}"}
        if jr.get("spam"):
            return {"ok": False, "reason": "spam_block"}
        if jr.get("feedback_required"):
            return {"ok": False, "reason": f"feedback_required: {jr.get('feedback_message','')[:120]}"}

        msg = jr.get("message") or jr.get("error_type") or str(jr)[:200]
        return {"ok": False, "reason": f"login_failed: {msg}"}


def _post_two_factor(client: httpx.Client, ua: dict, csrf: str, lsd: str,
                     identifier: str, code: str, username: str,
                     jazoest: str = "") -> dict:
    body = {
        "identifier": identifier,
        "isPrivacyPortalReq": "false",
        "queryParams": "{}",
        "trust_signal": "true",
        "username": username,
        "verification_method": "3",  # 3 = authenticator app
        "verificationCode": code,
        "jazoest": jazoest or _compute_jazoest(lsd),
    }
    headers = _build_headers(ua, csrf, lsd)
    headers["x-fb-friendly-name"] = ""
    headers.pop("x-fb-friendly-name", None)
    try:
        r = client.post(
            "https://www.instagram.com/api/v1/web/accounts/login/ajax/two_factor/",
            headers=headers, data=body)
    except Exception as e:
        return {"ok": False, "reason": f"2fa_post: {e}"}
    try:
        j = r.json()
    except Exception:
        return {"ok": False, "reason": f"2fa_bad_json: {r.text[:200]}"}
    if j.get("authenticated") or j.get("status") == "ok" and j.get("userId"):
        return {"ok": True}
    return {"ok": False, "reason": f"2fa_rejected: {r.text[:200]}"}


def _build_success(client: httpx.Client, ua: dict) -> dict:
    # Собираем все куки из jar'а (включая Set-Cookie от логина)
    cookies = {}
    for c in client.cookies.jar:
        if c.name and c.value:
            cookies[c.name] = c.value
    if not cookies.get("sessionid"):
        return {"ok": False, "reason": "no_sessionid_after_login"}
    return {
        "ok": True,
        "cookies": cookies,
        "user_agent": ua["ua"],
        "sec_ch_ua": ua["sec_ch_ua"],
        "sec_ch_ua_full": ua["sec_ch_ua_full"],
        "platform_version": ua["platform_version"],
        "browser": ua.get("browser", "chrome"),
        "www_claim": "",
    }
