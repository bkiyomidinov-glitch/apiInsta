/** REST API обёртки */
const BASE = '/api';

async function _fetch(method, path, body) {
    const opts = { method, headers: { 'Content-Type': 'application/json' } };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const r = await fetch(BASE + path, opts);
    if (!r.ok) throw new Error(`${method} ${path}: ${r.status}`);
    return r.json();
}
const get = (p) => _fetch('GET', p);
const post = (p, b) => _fetch('POST', p, b);
const put = (p, b) => _fetch('PUT', p, b);
const del = (p) => _fetch('DELETE', p);

export const getStats = () => get('/stats');
export const getAccounts = () => get('/accounts');
export const importAccounts = (text) => post('/accounts/import', { text });
export const deleteAccount = (u) => del(`/accounts/${encodeURIComponent(u)}`);

export const getSessions = () => get('/web-sessions');
export const deleteSession = (u) => del(`/web-sessions/${encodeURIComponent(u)}`);

export const getProxies = () => get('/web-proxies');
export const getAutoregConfig = () => get('/autoreg-config');
export const saveAutoregConfig = (config) => put('/autoreg-config', { config });

export const getPresets = () => get('/presets');
export const addPreset = (name, description) => post('/presets', { name, description });
export const deletePreset = (name) => del(`/presets/${encodeURIComponent(name)}`);

export const getVideoFolders = () => get('/video-folders');
export const getStoryFolders = () => get('/story-folders');
export const getLostAccounts = () => get('/lost-accounts');
export const getZalivLog = () => get('/zaliv-log');
export const getTasks = () => get('/tasks');
