"""User-Agent пул — Chrome 150 на разных версиях macOS (MacBook 2019+).

Версия сверена с реальным HAR браузерной заливки (Chrome 150.0.7871.x, macOS).
Chrome с ~107 применяет UA Reduction: в `user-agent` версия всегда `MAJOR.0.0.0`
(`150.0.0.0`), полный билд — ТОЛЬКО в `sec-ch-ua-full-version-list`. macOS-версия
в UA заморожена как `10_15_7`, реальная — в `sec-ch-ua-platform-version` (с точками).

sec-ch-ua формат Chrome 150 (verbatim из HAR): бренд-токен `"Not;A=Brand";v="8"`
ПЕРВЫМ, затем Chromium, затем Google Chrome.

ВАЖНО: версия Chrome (150) — это визуальный слой (UA/CH). TLS-таргет curl_cffi —
`chrome146` (max в 0.15), его JA3/JA4 совпадает с Chrome 150 (ClientHello стабилен).
При апгрейде curl_cffi до chrome150 синхронизировать `_IMPERSONATE_TARGET`.
"""
import hashlib

_UA_150 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")
_SEC_CH_UA_150 = '"Not;A=Brand";v="8", "Chromium";v="150", "Google Chrome";v="150"'


def _chrome150(full: str, plat_ver: str) -> dict:
    """Запись Chrome 150. `full` — полный билд ("150.0.7871.115") для
    sec-ch-ua-full-version-list; `plat_ver` — версия macOS С ТОЧКАМИ ('"15.4.1"')."""
    return {
        "ua": _UA_150,
        "sec_ch_ua": _SEC_CH_UA_150,
        "sec_ch_ua_full": (f'"Not;A=Brand";v="8.0.0.0", "Chromium";v="{full}", '
                           f'"Google Chrome";v="{full}"'),
        "platform_version": plat_ver,
        "browser": "chrome",
    }


UA_POOL = [
    # Chrome 150 на macOS Sequoia (15.x) — MacBook 2021+
    _chrome150("150.0.7871.115", '"15.4.1"'),
    _chrome150("150.0.7871.114", '"15.4.1"'),
    _chrome150("150.0.7871.115", '"15.4.0"'),
    _chrome150("150.0.7871.102", '"15.3.1"'),
    _chrome150("150.0.7871.101", '"15.3.0"'),
    _chrome150("150.0.7871.100", '"15.2.0"'),
    _chrome150("150.0.7871.49",  '"15.1.1"'),

    # Chrome 150 на macOS Sonoma (14.x) — MacBook 2020+
    _chrome150("150.0.7871.115", '"14.7.1"'),
    _chrome150("150.0.7871.114", '"14.6.1"'),
    _chrome150("150.0.7871.102", '"14.6.0"'),
    _chrome150("150.0.7871.101", '"14.5.0"'),
    _chrome150("150.0.7871.100", '"14.4.1"'),
    _chrome150("150.0.7871.49",  '"14.3.1"'),

    # Chrome 150 на macOS Ventura (13.x) — MacBook 2019+
    _chrome150("150.0.7871.115", '"13.7.1"'),
    _chrome150("150.0.7871.102", '"13.6.9"'),
    _chrome150("150.0.7871.49",  '"13.5.2"'),
]


def pick_ua_for_user(username: str) -> dict:
    """Стабильный выбор UA на основе hash — один юзер всегда получает один UA."""
    h = hashlib.sha1((username or "anon").lower().encode()).hexdigest()
    idx = int(h[:8], 16) % len(UA_POOL)
    return dict(UA_POOL[idx])


def pick_random_ua() -> dict:
    """Случайный UA — для анонимных запросов."""
    import random
    return dict(random.choice(UA_POOL))
