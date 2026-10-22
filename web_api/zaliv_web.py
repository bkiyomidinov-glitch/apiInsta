"""Батчевый залив Reels через Web API из сохранённых сессий.

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
    """Человеческий разброс задержки: seconds * uniform(1-spread, 1+spread).

    Фиксированные ровные паузы (ровно 2с/30с) — bot-сигнал; центр = заданное
    значение, но каждый раз чуть разный.
    """
    if seconds <= 0:
        return 0.0
    return round(seconds * random.uniform(1 - spread, 1 + spread), 2)


def run_zaliv_web(
    *,
    accounts: list,  # список usernames (сессии уже сохранены)
    description: str = "",
    videos_per_account: int = 1,
    video_folder: str | None = None,
    account_folders: dict | None = None,   # {username: folder_path}
    account_descriptions: dict | None = None,  # {username: desc}
    skip_accounts: list | None = None,
    unique_videos: bool = False,
    move_to_done: bool = True,
    uniqualize: bool = False,
    new_uniqalize: bool = False,
    delay_between_videos: int = 2,
    delay_after_all_videos: int = 30,
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

    _task_id = task_id or f"zaliv_web_{int(_time.time())}"

    # Фильтруем skip
    skip_set = set(s.lower() for s in (skip_accounts or []))
    flat_accounts = [u.strip().lower().lstrip("@") for u in accounts
                     if u and u.strip().lower().lstrip("@") not in skip_set]
    if not flat_accounts:
        log("❌ Список аккаунтов пуст")
        return _empty_result()

    total_accounts = len(flat_accounts)
    log(f"📦 Web-залив: {total_accounts} аккаунтов × {videos_per_account} видео")

    # Пресеты (__random__)
    _random_presets = None
    _description = description
    if description == "__random__":
        _random_presets = cfg.load_description_presets() or []
        if _random_presets:
            log(f"🎲 Рандомные описания: {len(_random_presets)} пресетов")
        else:
            log("⚠️ Нет пресетов — описание пустое")
            _description = ""

    def pick_desc(username):
        pad = account_descriptions or {}
        if isinstance(pad, dict) and username in pad:
            v = pad[username]
            if v: return v
        if _random_presets:
            return random.choice(_random_presets).get("description", "")
        return _description or ""

    def pick_folder(username):
        af = account_folders or {}
        return af.get(username) if isinstance(af, dict) else None

    # Видео пул
    def load_pool(folder):
        if folder:
            vids = cfg.get_pending_videos_from_folder(folder)
        else:
            vids = cfg.get_pending_videos()
        if unique_videos:
            vids = cfg.get_unreserved_videos(vids)
            random.shuffle(vids)
        return vids

    global_pool = load_pool(video_folder)
    pool_lock = threading.Lock()
    global_idx = [0]

    def next_video(per_account_folder):
        with pool_lock:
            if per_account_folder:
                pool = load_pool(per_account_folder)
                for _ in range(len(pool)):
                    if not pool: return None, None
                    vid = pool.pop(0)
                    if not os.path.exists(vid): continue
                    if cfg.reserve_video(vid, _task_id):
                        return vid, per_account_folder
                return None, None
            pool = global_pool
            if not pool: return None, None
            for _ in range(len(pool)):
                vid = pool[global_idx[0] % len(pool)]
                global_idx[0] += 1
                if not os.path.exists(vid): continue
                if cfg.reserve_video(vid, _task_id):
                    return vid, video_folder
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
    used_videos = {}
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

                desc = pick_desc(username)
                folder = pick_folder(username)

                account_success = 0
                session_broken = False
                current_vid = None

                for v_idx in range(1, videos_per_account + 1):
                    if _bool_stop(stop_check): break

                    if unique_videos or current_vid is None:
                        current_vid = next_video(folder)
                    if not current_vid or not current_vid[0]:
                        log(f"{tag}   ⚠️ Нет доступных видео для [{v_idx}/{videos_per_account}]")
                        break
                    vid_path, src = current_vid

                    per_upload_desc = pick_desc(username) if _random_presets else desc

                    upload_path = vid_path
                    tmp_path = None
                    try:
                        if new_uniqalize:
                            tmp_path = cfg.uniquify_video_new(vid_path, callback=log)
                        elif uniqualize:
                            tmp_path = cfg.uniquify_video_light(vid_path, callback=log)
                        if tmp_path: upload_path = tmp_path
                    except Exception as e:
                        log(f"{tag}   ⚠️ Уникализация: {e}")

                    name = os.path.basename(vid_path)
                    log(f"{tag}   🎬 [{v_idx}/{videos_per_account}] {name}")

                    upload_ok = False
                    try:
                        r = client.upload_reel(upload_path, per_upload_desc)
                        code = r.get("code") or "?"
                        log(f"{tag}   ✅ Опубликовано: code={code}")
                        upload_ok = True
                        account_success += 1
                        with result_lock:
                            result["videos_uploaded"] += 1
                        try:
                            cfg.append_zaliv_log_entry({
                                "timestamp": int(_time.time()),
                                "video": name, "video_path": str(vid_path),
                                "account": username,
                                "description_preview": (per_upload_desc or "")[:100],
                                "mode": "web", "media_code": code,
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
                    finally:
                        if tmp_path and tmp_path != vid_path:
                            try: Path(tmp_path).unlink(missing_ok=True)
                            except Exception: pass

                    if unique_videos:
                        if upload_ok and move_to_done:
                            try: cfg.move_video_to_done(vid_path, source_folder=src)
                            except Exception as e: log(f"{tag}   ⚠️ move_to_done: {e}")
                        elif upload_ok:
                            with used_lock: used_videos[vid_path] = src
                        else:
                            cfg.release_video(vid_path)

                    if session_broken: break
                    if v_idx < videos_per_account:
                        _interruptible_sleep(_jitter(delay_between_videos), stop_check)

                if not unique_videos and current_vid and current_vid[0]:
                    vid_path, src = current_vid
                    if account_success > 0:
                        with used_lock: used_videos[vid_path] = src
                    else:
                        cfg.release_video(vid_path)

                if account_success > 0:
                    with result_lock:
                        result["success_count"] += 1
                        result["accounts"].append(username)
                else:
                    with result_lock:
                        result["failed_accounts"].append(
                            {"username": username, "reason": "no_videos_uploaded"})
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
                _interruptible_sleep(_jitter(delay_after_all_videos), stop_check)

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

        if not unique_videos and move_to_done and used_videos:
            log(f"\n📦 Переношу {len(used_videos)} видео в done...")
            for vp, src in used_videos.items():
                try: cfg.move_video_to_done(vp, source_folder=src)
                except Exception as e: log(f"  ⚠️ {os.path.basename(vp)}: {e}")

        log(f"\n━━━ ИТОГ ━━━")
        log(f"  ✅ Успешных: {result['success_count']}")
        log(f"  ❌ Неудачных: {len(result['failed_accounts'])}")
        log(f"  🚫 Потеряно: {len(result['lost_accounts'])}")
        log(f"  🎬 Видео: {result['videos_uploaded']}")
    finally:
        cfg.release_videos_for_task(_task_id)

    return result


def _empty_result():
    return {
        "success_count": 0, "total": 0, "accounts": [],
        "videos_uploaded": 0, "lost_accounts": [],
        "failed_accounts": [], "mode": "web",
    }
