// api.js — one place for every call to the node.
//
// Auth is a bearer token (no login, no sessions): the browser keeps it in
// localStorage, scripts and the DDN client send the same header. A 401 is
// surfaced rather than retried, so a wrong token fails loudly instead of
// looking like an empty node.

const TOKEN_KEY = 'predpep.token';

export function getToken() {
  try { return localStorage.getItem(TOKEN_KEY) || ''; }
  catch { return ''; }          // private mode / blocked storage
}

export function setToken(value) {
  try {
    if (value) localStorage.setItem(TOKEN_KEY, value);
    else localStorage.removeItem(TOKEN_KEY);
  } catch { /* nothing we can do; calls will just be unauthenticated */ }
}

export class ApiError extends Error {
  constructor(message, status, payload) {
    super(message);
    this.status = status;
    this.payload = payload || {};
  }
}

function headers(extra) {
  const h = Object.assign({ 'Accept': 'application/json' }, extra || {});
  const t = getToken();
  if (t) h['Authorization'] = 'Bearer ' + t;
  return h;
}

async function parse(res) {
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); }
    catch { data = { raw: text }; }
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) ||
                (res.status === 401 ? 'Not authorised — check your API token.' :
                 res.status === 404 ? 'Not found.' :
                 'Request failed (' + res.status + ').');
    throw new ApiError(msg, res.status, data);
  }
  return data;
}

async function get(path) {
  return parse(await fetch(path, { headers: headers(), credentials: 'same-origin' }));
}

async function send(path, method, body) {
  const init = { method, headers: headers(), credentials: 'same-origin' };
  if (body instanceof FormData) {
    init.body = body;                       // let the browser set the boundary
  } else if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  return parse(await fetch(path, init));
}

/* ------------------------------------------------------------------ node */

export const api = {
  state:        ()          => get('/state'),
  health:       ()          => get('/health'),

  jobs:         ()          => get('/jobs'),
  job:          (id)        => get('/status/' + encodeURIComponent(id)),
  stopJob:      (id)        => send('/jobs/' + encodeURIComponent(id) + '/stop', 'POST'),
  deleteJob:    (id)        => send('/jobs/' + encodeURIComponent(id), 'DELETE'),

  // Per-iteration metrics the manager records as it goes (iteration_metrics.json).
  metrics:      (id)        => get('/jobs/' + encodeURIComponent(id) + '/metrics'),

  // Tail of the manager log. `bytes` caps how much is read from the end.
  log:          (id, bytes) => get('/jobs/' + encodeURIComponent(id) + '/log' +
                                   (bytes ? '?bytes=' + bytes : '')),

  results:      (id)        => get('/results_data/' + encodeURIComponent(id)),
  tmap:         (id)        => get('/get_tmap_tree/' + encodeURIComponent(id)),

  // Everything the Analytics view draws, computed server-side with pandas:
  // score evolution, FoldX/Rosetta agreement, term correlation, PCA, logo.
  analytics:    (id)        => get('/jobs/' + encodeURIComponent(id) + '/analytics'),

  // The design residue palette: the residue CSV intersected with the params
  // actually installed, so the form can only offer residues Rosetta can load.
  residues:     ()          => get('/api/residues'),

  submit:       (formData)  => send('/upload', 'POST', formData),

  pdbUrl:       (relPath)   => '/stream_final_pdb/' + relPath,
  zipUrl:       (id)        => '/download/' + encodeURIComponent(id) + '/' +
                               encodeURIComponent(id) + '.zip',

  tokens:       ()          => get('/api/tokens'),
  issueToken:   (body)      => send('/api/tokens', 'POST', body),
  revokeToken:  (tid)       => send('/api/tokens/' + encodeURIComponent(tid), 'DELETE'),
  audit:        ()          => get('/api/audit'),
};

/* ----------------------------------------------------------------- utils */

export function fmtBytes(n) {
  if (!n && n !== 0) return '—';
  const gb = n / (1024 ** 3);
  if (gb >= 1) return gb.toFixed(1) + ' GB';
  return (n / (1024 ** 2)).toFixed(0) + ' MB';
}

export function fmtWhen(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return d.toLocaleString([], {
    year: '2-digit', month: 'short', day: '2-digit',
    hour: '2-digit', minute: '2-digit'
  });
}

export function fmtElapsed(fromIso, toIso) {
  if (!fromIso) return '—';
  const a = new Date(fromIso).getTime();
  const b = toIso ? new Date(toIso).getTime() : Date.now();
  if (isNaN(a) || isNaN(b) || b < a) return '—';
  const mins = Math.floor((b - a) / 60000);
  const h = Math.floor(mins / 60);
  return h > 0 ? h + ' h ' + String(mins % 60).padStart(2, '0') + ' m' : mins + ' m';
}

export function fmtNum(v, digits = 2) {
  const n = parseFloat(v);
  return isFinite(n) ? n.toFixed(digits) : '—';
}

export function esc(s) {
  return String(s == null ? '—' : s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

/* Poll `fn` every `ms`, pausing while the tab is hidden. Returns a stop(). */
export function poll(fn, ms) {
  let timer = null, stopped = false;
  const tick = async () => {
    if (stopped) return;
    if (!document.hidden) {
      try { await fn(); } catch (e) { console.warn('[poll]', e.message); }
    }
    if (!stopped) timer = setTimeout(tick, ms);
  };
  tick();
  return () => { stopped = true; if (timer) clearTimeout(timer); };
}
