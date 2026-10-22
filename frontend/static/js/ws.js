/** WebSocket клиент с каналами */
let _ws = null;
let _subs = {};   // channel -> [cb, ...]
let _queue = [];  // messages to send when reconnected
let _reconnectTimeout = null;

function _connect() {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _ws = new WebSocket(`${proto}//${location.host}/ws`);
    _ws.onopen = () => {
        document.querySelectorAll('.au-ws-status').forEach(el => {
            el.textContent = '● online';
            el.style.color = 'var(--au-mint)';
            el.style.borderColor = 'rgba(78,255,159,0.4)';
        });
        while (_queue.length) _ws.send(JSON.stringify(_queue.shift()));
    };
    _ws.onclose = () => {
        document.querySelectorAll('.au-ws-status').forEach(el => {
            el.textContent = '● offline';
            el.style.color = 'var(--au-red)';
            el.style.borderColor = 'rgba(255,78,125,0.4)';
        });
        _reconnectTimeout = setTimeout(_connect, 2000);
    };
    _ws.onerror = (e) => console.error('[ws]', e);
    _ws.onmessage = (ev) => {
        try {
            const data = JSON.parse(ev.data);
            const ch = data.channel || '*';
            for (const cb of (_subs[ch] || [])) cb(data);
            for (const cb of (_subs['*'] || [])) cb(data);
        } catch (e) { console.error(e); }
    };
}

export function send(payload) {
    if (!_ws || _ws.readyState !== 1) {
        _queue.push(payload);
        return false;
    }
    _ws.send(JSON.stringify(payload));
    return true;
}

export function subscribe(channel, cb) {
    if (!_subs[channel]) _subs[channel] = [];
    _subs[channel].push(cb);
    return () => {
        _subs[channel] = (_subs[channel] || []).filter(c => c !== cb);
    };
}

_connect();
