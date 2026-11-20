// admin.js — Admin & audit: API tokens and the action log.
//
// Tokens are shown exactly once, at creation. The node stores only a hash, so
// a lost token is replaced rather than recovered.

import { api, esc, fmtWhen, getToken, setToken } from './api.js';

const SCOPES = [
  ['read',   'Read only — jobs, results, analytics'],
  ['submit', 'Submit and read'],
  ['admin',  'Full — including stop, delete and token management']
];

function renderTokenField() {
  const input = document.getElementById('tokenInput');
  if (input) input.value = getToken();
}

async function renderTokens() {
  const body = document.getElementById('tokensBody');
  if (!body) return;
  try {
    const res = await api.tokens();
    const rows = (res && res.tokens) || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="6" class="empty">No tokens issued yet.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(t => `<tr>
      <td><strong>${esc(t.label)}</strong><span class="sub-note mono">${esc(t.prefix || '')}…</span></td>
      <td>${esc(t.owner)}</td>
      <td><span class="badge ${t.scope === 'admin' ? 'partial' : 'queued'}">${esc(t.scope)}</span></td>
      <td>${fmtWhen(t.created_at)}</td>
      <td>${t.last_used_at ? fmtWhen(t.last_used_at) : '<span class="muted">never</span>'}</td>
      <td style="text-align:right">
        <button class="btn-sm btn-danger" data-revoke="${esc(t.id)}">Revoke</button>
      </td>
    </tr>`).join('');
  } catch (e) {
    body.innerHTML = '<tr><td colspan="6" class="empty">' +
      (e.status === 401
        ? 'An admin token is required to manage tokens. Paste one on the right.'
        : 'Could not load tokens: ' + esc(e.message)) +
      '</td></tr>';
  }
}

async function renderAudit() {
  const body = document.getElementById('auditBody');
  if (!body) return;
  try {
    const res = await api.audit();
    const rows = (res && res.entries) || [];
    if (!rows.length) {
      body.innerHTML = '<tr><td colspan="3" class="empty">Nothing recorded yet.</td></tr>';
      return;
    }
    body.innerHTML = rows.map(e => `<tr>
      <td class="small">${fmtWhen(e.at)}</td>
      <td class="small">${esc(e.actor)}</td>
      <td class="small">${esc(e.action)}${e.target ? ` <span class="mono">${esc(e.target)}</span>` : ''}</td>
    </tr>`).join('');
  } catch (e) {
    body.innerHTML = '<tr><td colspan="3" class="empty">' +
      (e.status === 401 ? 'Requires an admin token.' : 'Could not load history: ' + esc(e.message)) +
      '</td></tr>';
  }
}

/* The new token is rendered into the page once and never fetched again. */
function showNewToken(token, label) {
  const host = document.getElementById('tokensBody');
  if (!host) return;
  const tr = document.createElement('tr');
  tr.innerHTML = `<td colspan="6">
    <div class="alert alert-ok">
      <strong>Token for “${esc(label)}” — copy it now.</strong>
      <code class="mono" style="display:block;margin:8px 0;word-break:break-all;
            background:#fff;padding:8px;border-radius:4px">${esc(token)}</code>
      This is the only time it is shown; the node keeps only a hash.
      <button class="btn-sm" data-copy="${esc(token)}" style="margin-top:6px">Copy</button>
    </div></td>`;
  host.prepend(tr);
}

async function issueToken() {
  const label = prompt('Label for this token (e.g. "DDN client", "kapila laptop"):');
  if (!label) return;
  const owner = prompt('Owner (name or system):', label) || label;
  const scope = prompt('Scope — ' + SCOPES.map(s => s[0]).join(' / ') + ':', 'submit');
  if (!scope || !SCOPES.some(s => s[0] === scope)) {
    if (scope) alert('Unknown scope: ' + scope);
    return;
  }
  try {
    const res = await api.issueToken({ label, owner, scope });
    if (!res || !res.success) throw new Error((res && res.error) || 'Could not issue a token.');
    await renderTokens();
    showNewToken(res.token, label);
    renderAudit();
  } catch (e) {
    alert(e.message);
  }
}

export function init() {
  renderTokenField();

  document.getElementById('saveToken')?.addEventListener('click', async () => {
    const input = document.getElementById('tokenInput');
    setToken(input ? input.value.trim() : '');
    await Promise.all([renderTokens(), renderAudit()]);
  });

  document.getElementById('clearToken')?.addEventListener('click', async () => {
    setToken('');
    renderTokenField();
    await Promise.all([renderTokens(), renderAudit()]);
  });

  document.getElementById('tokenInput')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') document.getElementById('saveToken')?.click();
  });

  document.getElementById('newToken')?.addEventListener('click', issueToken);

  document.getElementById('tokensBody')?.addEventListener('click', async e => {
    const copy = e.target.closest('[data-copy]');
    if (copy) {
      try {
        await navigator.clipboard.writeText(copy.dataset.copy);
        copy.textContent = 'Copied';
      } catch {
        copy.textContent = 'Select the text above to copy';
      }
      return;
    }
    const rev = e.target.closest('[data-revoke]');
    if (!rev) return;
    if (!confirm('Revoke this token? Anything using it stops working immediately.')) return;
    try {
      await api.revokeToken(rev.dataset.revoke);
      await renderTokens();
      renderAudit();
    } catch (err) {
      alert(err.message);
    }
  });
}

export function show() {
  renderTokenField();
  renderTokens();
  renderAudit();
}
export function hide() {}
