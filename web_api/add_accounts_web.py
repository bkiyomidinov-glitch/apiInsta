"""Батчевый логин аккаунтов через Instagram Web API + сохранение сессий."""
import logging

logger = logging.getLogger(__name__)


def _bool_stop(stop_check):
    try:
        return bool(stop_check()) if stop_check else False
    except Exception:
        return False


def add_accounts_web_batch(
    accounts: list,
    *,
    callback=None,
    stop_check=None,
    use_proxies: bool = False,
    task_id: str = None,
) -> dict:
    """Логинит список аккаунтов и сохраняет сессии.

    accounts: [{username, password, otp_secret}]
    """
    import config as cfg
    from .api_login import api_login
    from .ig_web_engine import rotate_proxy_ip

    def log(msg):
        if callback:
            try: callback(msg)
            except Exception: pass
        else:
            logger.info(msg)

    total = len(accounts)
    success, failed, lost = [], [], []

    proxy_infos = []
    proxy_urls = []
    if use_proxies:
        proxy_infos = cfg.load_autoreg_proxies() or []
        proxy_urls = [p["url"] for p in proxy_infos]
        if proxy_urls:
            log(f"🌐 Прокси: {len(proxy_urls)} шт — ротация по аккам + смена IP")
        else:
            log("⚠️ Прокси не настроены")

    log(f"📦 Web логин: {total} аккаунтов")

    for i, acc in enumerate(accounts, 1):
        if _bool_stop(stop_check):
            log("⛔ Остановлено"); break

        u = (acc.get("username") or "").strip().lower().lstrip("@")
        pwd = acc.get("password") or ""
        otp = (acc.get("otp_secret") or acc.get("otp") or "").replace(" ", "")
        if not u or not pwd:
            failed.append({"username": u or "?", "reason": "missing credentials"})
            continue

        if cfg.has_valid_web_session(u, max_age_days=14):
            log(f"[{i}/{total}] ✅ {u}: уже есть свежая сессия — пропускаю")
            success.append(u)
            # Upsert в accounts.json в любом случае
            cfg.upsert_account(u, pwd, otp)
            continue

        if not otp:
            otp = cfg.get_otp_secret(u) or ""

        proxy_idx = (i - 1) % len(proxy_urls) if proxy_urls else 0
        proxy_url = proxy_urls[proxy_idx] if proxy_urls else None
        proxy_info = proxy_infos[proxy_idx] if proxy_infos else None

        if proxy_info and proxy_info.get("change_ip_url"):
            log(f"\n[{i}/{total}] 🔁 Смена IP перед {u}...")
            rotate_proxy_ip(proxy_info, wait_sec=12.0, log=log, stop_check=stop_check)

        if proxy_url:
            log(f"[{i}/{total}] 🔐 {u} — логин (через {proxy_url.split('@')[-1]})...")
        else:
            log(f"[{i}/{total}] 🔐 {u} — логин через API...")

        try:
            r = api_login(username=u, password=pwd, otp_secret=otp,
                          proxy=proxy_url, log=log)
        except Exception as e:
            r = {"ok": False, "reason": f"exception: {e}"}

        if r.get("ok"):
            cfg.save_web_session(
                u, r["cookies"],
                user_agent=r.get("user_agent", ""),
                www_claim=r.get("www_claim", ""),
                sec_ch_ua=r.get("sec_ch_ua", ""),
                sec_ch_ua_full=r.get("sec_ch_ua_full", ""),
                platform_version=r.get("platform_version", ""),
                browser=r.get("browser", ""),
                proxy=proxy_url or "",
            )
            cfg.upsert_account(u, pwd, otp)
            success.append(u)
            log(f"[{i}/{total}] ✅ {u}: сессия сохранена")
        else:
            reason = r.get("reason", "unknown")
            if any(m in reason for m in ("account_disabled", "challenge_required",
                                          "rate_limited", "checkpoint")):
                lost.append({"username": u, "reason": reason})
                log(f"[{i}/{total}] 🚫 {u}: {reason}")
                try: cfg.log_lost_account(u, reason)
                except Exception: pass
            else:
                failed.append({"username": u, "reason": reason})
                log(f"[{i}/{total}] ❌ {u}: {reason}")

    log(f"\n━━━ ИТОГ ━━━")
    log(f"  ✅ Успешно: {len(success)}")
    log(f"  ❌ Ошибок: {len(failed)}")
    log(f"  🚫 Потеряно (disabled/challenge): {len(lost)}")

    return {
        "success_count": len(success),
        "fail_count": len(failed),
        "success_accounts": success,
        "failed_accounts": failed,
        "lost_accounts": lost,
        "total": total,
        "mode": "web",
    }
