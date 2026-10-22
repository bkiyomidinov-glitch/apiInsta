/** Инжектирует aurora-навбар + утилиты toast/confirm. */

export function injectNavbar(activePage = '') {
    const items = [
        { key: 'home',     href: '/',         icon: '🏠', label: 'главная' },
        { key: 'accounts', href: '/accounts', icon: '👥', label: 'аккаунты' },
        { key: 'zaliv',    href: '/zaliv',    icon: '🚀', label: 'залив' },
        { key: 'stories',  href: '/stories',  icon: '📖', label: 'сторис' },
        { key: 'sessions', href: '/sessions', icon: '🔑', label: 'сессии' },
        { key: 'settings', href: '/settings', icon: '⚙️', label: 'настройки' },
    ];

    const nav = document.createElement('nav');
    nav.className = 'au-nav';
    let html = `
        <div class="au-nav-brand">
            <div class="au-nav-logo"></div>
            <div class="au-nav-name">instapi</div>
        </div>`;
    for (const it of items) {
        html += `<a class="au-nav-link ${activePage === it.key ? 'active' : ''}" href="${it.href}">${it.icon} <span>${it.label}</span></a>`;
    }
    html += `<a class="au-nav-tg" href="https://t.me/ubtkormittt" target="_blank" rel="noopener" title="мой Telegram-канал">✈️ <span>канал</span></a>`;
    html += `<span class="au-ws-status">● connecting</span>`;
    nav.innerHTML = html;
    document.body.insertBefore(nav, document.body.firstChild);

    const bg = document.createElement('div');
    bg.className = 'au-aurora';
    bg.innerHTML = `
        <div class="au-blob au-blob-1"></div>
        <div class="au-blob au-blob-2"></div>
        <div class="au-blob au-blob-3"></div>
        <div class="au-blob au-blob-4"></div>`;
    document.body.insertBefore(bg, document.body.firstChild);
}

export function showToast(message, type = 'info', duration = 3500) {
    const t = document.createElement('div');
    t.className = 'au-toast ' + type;
    t.textContent = message;
    document.body.appendChild(t);
    setTimeout(() => {
        t.style.opacity = '0';
        t.style.transition = 'opacity 0.3s';
    }, duration);
    setTimeout(() => t.remove(), duration + 400);
}

export function confirmAction(message) {
    return new Promise(resolve => {
        const overlay = document.createElement('div');
        overlay.className = 'au-overlay';
        overlay.innerHTML = `
            <div class="au-card" style="max-width: 420px; width: calc(100vw - 40px); padding: 24px;">
                <div style="font-size: 15px; margin-bottom: 20px; line-height: 1.4;">${message}</div>
                <div class="au-row" style="gap: 8px; justify-content: flex-end;">
                    <button class="au-btn au-btn-glass au-btn-sm" data-a="0">отмена</button>
                    <button class="au-btn au-btn-primary au-btn-sm" data-a="1">ок</button>
                </div>
            </div>`;
        overlay.addEventListener('click', e => {
            if (e.target === overlay) { resolve(false); overlay.remove(); }
        });
        overlay.querySelectorAll('button').forEach(b => b.addEventListener('click', () => {
            resolve(b.dataset.a === '1');
            overlay.remove();
        }));
        document.body.appendChild(overlay);
    });
}
