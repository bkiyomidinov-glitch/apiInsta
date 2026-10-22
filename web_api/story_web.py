"""Батчевый залив Stories через Web API из сохранённых сессий.

Порт логики из расширения my_story_ext: rupload_ig{photo,video} → configure_to_story.
Параллельно по прокси (1 прокси = 1 поток). UA закреплён за аккаунтом навсегда.
"""
import logging
import os
import random
import threading
import time as _time
from pathlib import Path

logger = logging.getLogger(__name__)


def _bool_stop(stop_check):
    try:
        return bool(stop_check()) if stop_check else False
    except Exception:
        return False


def _interruptible_sleep(seconds: float, stop_check):
    end = _time.time() + max(0, seconds)
    while _time.time() < end:
        if _bool_stop(stop_check):
            return
        _time.sleep(min(1.0, end - _time.time()))


def _jitter(seconds: float, spread: float = 0.3) -> float:
    """Человеческий разброс задержки: центр = заданное значение, ±spread."""
    if seconds <= 0:
        return 0.0
    return round(seconds * random.uniform(1 - spread, 1 + spread), 2)


def _normalize_url(url: str) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if not raw.lower().startswith(("http://", "https://")):
        raw = "https://" + raw
    return raw


def run_story_web(
    *,
    accounts: list,  # список usernames (сессии уже сохранены)
    stories_per_account: int = 1,
    media_folder: str | None = None,
    account_folders: dict | None = None,   # {username: folder_path}
    skip_accounts: list | None = None,
    unique_media: bool = False,
    move_to_done: bool = True,
    besties: bool = False,
    links: list | None = None,       # список URL (раздаётся по аккаунтам)
    link_url: str = "",              # одиночная ссылка (back-compat)
    link_label: str = "",            # подпись на стикере (пусто → домен)
    unique_links: bool = False,      # True → 1 ссылка на акк; False → первая на всех
    link_pos: dict | None = None,    # {"x":0.5,"y":0.82,"scale":1.0}
    link_variant: str = "default",   # default | white | solid | black
    delay_between_stories: int = 2,
    delay_after_all: int = 30,
    use_proxies: bool = False,
    callback=None,
    stop_check=None,
    task_id: str = None,
    **_ignored,
) -> dict:
    import config as cfg
    from .ig_web_engine import (IgWebClient, WebUploadError, WebChallengeError,
                                rotate_proxy_ip, accept_language_for_proxy)
    from .ua_pool import pick_ua_for_user

    def log(msg):
        if callback:
            try: callback(msg)
            except Exception: pass
        else:
            logger.info(msg)

    _task_id = task_id or f"story_web_{int(_time.time())}"

    # Фильтруем skip
    skip_set = set(s.lower() for s in (skip_accounts or []))
    flat_accounts = [u.strip().lower().lstrip("@") for u in accounts
                     if u and u.strip().lower().lstrip("@") not in skip_set]
    if not flat_accounts:
        log("❌ Список аккаунтов пуст")
        return _empty_result()

    total_accounts = len(flat_accounts)
    log(f"📖 Web-сторис: {total_accounts} аккаунтов × {stories_per_account} сторис")
    if besties:
        log("👥 Аудитория: близкие друзья")

    # ── Link-стикеры: список + позиция + раздача по аккаунтам ──
    raw_links = [l for l in (links or []) if l and l.strip()]
    if not raw_links and link_url.strip():
        raw_links = [link_url]
    norm_links = [u for u in (_normalize_url(l) for l in raw_links) if u]

    lp = link_pos or {}
    pos = {
        "x": min(0.95, max(0.05, float(lp.get("x", 0.5)))),
        "y": min(0.95, max(0.05, float(lp.get("y", 0.82)))),
        "scale": min(3.0, max(0.3, float(lp.get("scale", 1.0)))),
    }
    base_w = round(0.42 * pos["scale"], 4)
    base_h = round(0.075 * pos["scale"], 4)

    # username -> url (стабильно, до разбивки по воркерам)
    account_link = {}
    if norm_links:
        if unique_links:
            for i, u in enumerate(flat_accounts):
                account_link[u] = norm_links[i % len(norm_links)]
            if len(norm_links) < total_accounts:
                log(f"🔗 Ссылок ({len(norm_links)}) меньше акков ({total_accounts}) — раздаю по кругу")
            else:
                log(f"🔗 Уникальные ссылки: 1 на акк ({len(norm_links)} шт)")
        else:
            for u in flat_accounts:
                account_link[u] = norm_links[0]
            log(f"🔗 Одна ссылка на все акки: {norm_links[0]}")
        log(f"🔗 Позиция стикера: x={pos['x']:.2f} y={pos['y']:.2f} размер×{pos['scale']:.2f}")

    def pick_folder(username):
        af = account_folders or {}
        return af.get(username) if isinstance(af, dict) else None

    # Медиа пул (фото + видео)
    def load_pool(folder):
        if folder:
            media = cfg.get_pending_stories_from_folder(folder)
        else:
            media = cfg.get_pending_stories()
        if unique_media:
            media = cfg.get_unreserved_videos(media)
            random.shuffle(media)
        return media

    global_pool = load_pool(media_folder)
    pool_lock = threading.Lock()
    global_idx = [0]

    def next_media(per_account_folder):
        with pool_lock:
            if per_account_folder:
                pool = load_pool(per_account_folder)
                for _ in range(len(pool)):
                    if not pool: return None, None
                    m = pool.pop(0)
                    if not os.path.exists(m): continue
                    if cfg.reserve_video(m, _task_id):
                        return m, per_account_folder
                return None, None
            pool = global_pool
            if not pool: return None, None
            for _ in range(len(pool)):
                m = pool[global_idx[0] % len(pool)]
                global_idx[0] += 1
                if not os.path.exists(m): continue
                if cfg.reserve_video(m, _task_id):
                    return m, media_folder
            return None, None

    # Прокси
    proxies = cfg.load_autoreg_proxies() if use_proxies else []
    if proxies:
        num_workers = len(proxies)  # 1 прокси = 1 параллельный поток (без ограничения)
        chunks = [flat_accounts[i::num_workers] for i in range(num_workers)]
        worker_proxies = [proxies[i] for i in range(num_workers)]
        proxy_urls = [p["url"] for p in worker_proxies]
        log(f"🌐 Прокси активны: {num_workers} поток(а/ов)")
    else:
        num_workers = 1
        chunks = [flat_accounts]
        worker_proxies = [None]
        proxy_urls = [None]
        if use_proxies:
            log("⚠️ Прокси не настроены — работаю в 1 поток без прокси")

    result = _empty_result()
    result["total"] = total_accounts
    result_lock = threading.Lock()
    used_media = {}
    used_lock = threading.Lock()

    def worker(worker_idx, my_accounts, my_proxy, my_proxy_info):
        tag = f"[W{worker_idx+1}]"
        if my_proxy:
            log(f"{tag} 🌐 прокси {my_proxy.split('@')[-1]} ({len(my_accounts)} акк)")
        else:
            log(f"{tag} без прокси ({len(my_accounts)} акк)")

        # Accept-Language по гео прокси (одна геолокация на воркер, кэш в движке).
        worker_lang = accept_language_for_proxy(my_proxy, log=lambda m: log(f"{tag} {m}"))

        for a_i, username in enumerate(my_accounts, 1):
            if _bool_stop(stop_check):
                log(f"{tag} ⛔ stop"); break

            if my_proxy_info and my_proxy_info.get("change_ip_url"):
                log(f"\n{tag} 🔁 Перед {username}: смена IP...")
                rotate_proxy_ip(my_proxy_info, wait_sec=12.0,
                                log=lambda m: log(f"{tag}{m}"), stop_check=stop_check)

            sess = cfg.load_web_session(username)
            if not sess or not sess.get("cookies", {}).get("sessionid"):
                log(f"{tag} [{a_i}/{len(my_accounts)}] ❌ {username}: нет web-сессии")
                with result_lock:
                    result["failed_accounts"].append(
                        {"username": username, "reason": "no_web_session"})
                continue

            log(f"\n{tag} [{a_i}/{len(my_accounts)}] 🎯 {username}")

            # UA sticky
            saved_ua = sess.get("user_agent") or ""
            has_full_ua = ("Chrome/" in saved_ua and sess.get("sec_ch_ua")
                           and sess.get("sec_ch_ua_full") and sess.get("platform_version"))
            if has_full_ua:
                ua_info = {
                    "ua": saved_ua, "sec_ch_ua": sess["sec_ch_ua"],
                    "sec_ch_ua_full": sess["sec_ch_ua_full"],
                    "platform_version": sess["platform_version"],
                    "browser": sess.get("browser", ""),
                }
            else:
                ua_info = pick_ua_for_user(username)
                log(f"{tag}   🪪 Закрепляю UA: Chrome {ua_info['ua'].split('Chrome/')[1].split(' ')[0]}")

            ck = {
                "cookies": sess["cookies"],
                "user_agent": ua_info["ua"],
                "sec_ch_ua": ua_info["sec_ch_ua"],
                "sec_ch_ua_full": ua_info["sec_ch_ua_full"],
                "platform_version": ua_info["platform_version"],
                "accept_language": worker_lang,
            }
            if sess.get("www_claim"): ck["www_claim"] = sess["www_claim"]
            if my_proxy: ck["proxy"] = my_proxy
            client = IgWebClient(**ck)

            session_was_valid = False
            try:
                st = client.session_status()
                if st == "challenge":
                    # Восстановимо — акк ЖИВ, НЕ удалять, пропускаем этот прогон.
                    log(f"{tag}   🚫 challenge/чекпоинт — акк жив, пропускаю (не удаляю)")
                    with result_lock:
                        result["failed_accounts"].append(
                            {"username": username, "reason": "challenge"})
                    continue
                if st != "ok":
                    log(f"{tag}   🚫 Сессия протухла ({st})")
                    try: cfg.log_lost_account(username, "web_session_expired")
                    except Exception: pass
                    with result_lock:
                        result["lost_accounts"].append(
                            {"username": username, "reason": "web_session_expired"})
                    continue
                session_was_valid = True

                folder = pick_folder(username)
                u_link_url = account_link.get(username)
                link_obj = None
                if u_link_url:
                    # width/height здесь — запасные; движок пересчитает их по
                    # реально отрисованному стикеру (визуал вшивается в медиа).
                    link_obj = {
                        "url": u_link_url,
                        "x": pos["x"], "y": pos["y"],
                        "width": base_w, "height": base_h, "rotation": 0.0,
                        "scale": pos["scale"], "label": link_label,
                        "variant": link_variant,
                    }
                    log(f"{tag}   🔗 {u_link_url}")
                account_success = 0
                session_broken = False
                current_media = None

                for s_idx in range(1, stories_per_account + 1):
                    if _bool_stop(stop_check): break

                    if unique_media or current_media is None:
                        current_media = next_media(folder)
                    if not current_media or not current_media[0]:
                        log(f"{tag}   ⚠️ Нет доступных медиа для [{s_idx}/{stories_per_account}]")
                        break
                    media_path, src = current_media

                    name = os.path.basename(media_path)
                    log(f"{tag}   📖 [{s_idx}/{stories_per_account}] {name}")

                    upload_ok = False
                    try:
                        r = client.upload_story(media_path, besties=besties, link=link_obj)
                        code = r.get("code") or "?"
                        log(f"{tag}   ✅ Сторис опубликована: code={code}")
                        upload_ok = True
                        account_success += 1
                        with result_lock:
                            result["stories_uploaded"] += 1
                        try:
                            cfg.append_zaliv_log_entry({
                                "timestamp": int(_time.time()),
                                "video": name, "video_path": str(media_path),
                                "account": username,
                                "mode": "story", "media_code": code,
                            })
                        except Exception: pass
                        try:
                            cfg.save_web_session(
                                username, client.current_cookies(),
                                user_agent=ua_info["ua"],
                                www_claim=(client.www_claim if client.www_claim not in ("", "0")
                                           else sess.get("www_claim", "")),
                                sec_ch_ua=ua_info["sec_ch_ua"],
                                sec_ch_ua_full=ua_info["sec_ch_ua_full"],
                                platform_version=ua_info["platform_version"],
                                browser=ua_info.get("browser") or sess.get("browser", ""),
                                proxy=my_proxy or sess.get("proxy", ""),
                            )
                        except Exception: pass
                    except WebChallengeError as e:
                        # Восстановимо (checkpoint/спам/429/5xx): акк ЖИВ — НЕ удалять,
                        # прекращаем заливать на него в этом прогоне.
                        log(f"{tag}   🚫 challenge/лимит: {e} — акк жив, пропускаю")
                        session_broken = True
                    except WebUploadError as e:
                        log(f"{tag}   ❌ upload: {e}")
                        if "session invalid" in str(e).lower():
                            try: cfg.log_lost_account(username, "web_session_expired")
                            except Exception: pass
                            with result_lock:
                                result["lost_accounts"].append(
                                    {"username": username, "reason": "web_session_expired"})
                            session_broken = True
                    except Exception as e:
                        log(f"{tag}   ❌ unexpected: {e}")

                    if unique_media:
                        if upload_ok and move_to_done:
                            try: cfg.move_story_to_done(media_path, source_folder=src)
                            except Exception as e: log(f"{tag}   ⚠️ move_to_done: {e}")
                        elif upload_ok:
                            with used_lock: used_media[media_path] = src
                        else:
                            cfg.release_video(media_path)

                    if session_broken: break
                    if s_idx < stories_per_account:
                        _interruptible_sleep(_jitter(delay_between_stories), stop_check)

                if not unique_media and current_media and current_media[0]:
                    media_path, src = current_media
                    if account_success > 0:
                        with used_lock: used_media[media_path] = src
                    else:
                        cfg.release_video(media_path)

                if account_success > 0:
                    with result_lock:
                        result["success_count"] += 1
                        result["accounts"].append(username)
                else:
                    with result_lock:
                        result["failed_accounts"].append(
                            {"username": username, "reason": "no_stories_uploaded"})
            finally:
                if session_was_valid:
                    try:
                        cfg.save_web_session(
                            username, client.current_cookies(),
                            user_agent=ua_info["ua"],
                            www_claim=(client.www_claim if client.www_claim not in ("", "0")
                                       else sess.get("www_claim", "")),
                            sec_ch_ua=ua_info["sec_ch_ua"],
                            sec_ch_ua_full=ua_info["sec_ch_ua_full"],
                            platform_version=ua_info["platform_version"],
                            browser=ua_info.get("browser") or sess.get("browser", ""),
                            proxy=my_proxy or sess.get("proxy", ""),
                        )
                    except Exception: pass
                client.close()

            if a_i < len(my_accounts):
                _interruptible_sleep(_jitter(delay_after_all), stop_check)

    try:
        if num_workers == 1:
            worker(0, chunks[0], proxy_urls[0], worker_proxies[0])
        else:
            threads = []
            for idx, (chunk, purl, pinfo) in enumerate(zip(chunks, proxy_urls, worker_proxies)):
                t = threading.Thread(target=worker, args=(idx, chunk, purl, pinfo), daemon=True)
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

        if not unique_media and move_to_done and used_media:
            log(f"\n📦 Переношу {len(used_media)} медиа в done...")
            for mp, src in used_media.items():
                try: cfg.move_story_to_done(mp, source_folder=src)
                except Exception as e: log(f"  ⚠️ {os.path.basename(mp)}: {e}")

        log(f"\n━━━ ИТОГ ━━━")
        log(f"  ✅ Успешных: {result['success_count']}")
        log(f"  ❌ Неудачных: {len(result['failed_accounts'])}")
        log(f"  🚫 Потеряно: {len(result['lost_accounts'])}")
        log(f"  📖 Сторис: {result['stories_uploaded']}")
    finally:
        cfg.release_videos_for_task(_task_id)

    return result


def _empty_result():
    return {
        "success_count": 0, "total": 0, "accounts": [],
        "stories_uploaded": 0, "lost_accounts": [],
        "failed_accounts": [], "mode": "story",
    }
