// ops.js — Operations view: capacity, queue, job list, live detail + log tail.

import { api, poll, esc, fmtBytes, fmtWhen, fmtElapsed, fmtNum } from './api.js';
import { renderLine } from './charts.js';

const FILTERS = ['All', 'Running', 'Queued', 'Complete', 'Partial', 'Failed'];

const state = {
  jobs: [],
  maxIterations: 6,
  filter: 'All',
  search: '',
  selected: null,
  stopPoll: null,
  stopLogPoll: null,
  started: false
};

/* A partial job may still report status "Complete" on the wire while the
   PREDPEP_PARTIAL_STATUS gate is off, so the label trusts the flag. */
function label(job) {
  return job.partial ? 'Partial' : (job.status || 'Running');
}

function badge(job) {
  const l = label(job);
  return '<span class="badge ' + l.toLowerCase() + '">' + esc(l) + '</span>';
}

function iterCells(job) {
  const max = state.maxIterations;
  const done = Number(job.iteration) || 0;
  const running = job.status === 'Running';
  let html = '<div class="iters" style="grid-template-columns:repeat(' + max + ',1fr)">';
  for (let i = 0; i < max; i++) {
    const cls = i < done ? 'done' : (i === done && running ? 'now' : '');
    html += '<i class="' + cls + '"></i>';
  }
  html += '</div>';
  const reason = job.finish_reason === 'limit' ? ' · limit reached'
               : job.finish_reason === 'early' ? ' · early stop' : '';
  const text = job.iteration == null
    ? (job.status === 'Queued' ? 'not started' : '—')
    : done + ' / ' + max + reason;
  return html + '<span class="sub-note">' + esc(text) + '</span>';
}

function visible() {
  const q = state.search.trim().toLowerCase();
  return state.jobs.filter(j => {
    if (state.filter !== 'All' && label(j) !== state.filter) return false;
    if (!q) return true;
    // parent_job is included so searching the id of a split submission finds
    // every sub-job derived from it, not none of them: the parent id appears in
    // no sub-job's own job_id beyond a shared prefix.
    return [j.job_id, j.protein_symbol, j.user_name,
            j.segment && j.segment.parent_job].some(
      v => String(v || '').toLowerCase().includes(q));
  });
}

/* ------------------------------------------------------------------ stats */

function renderStats(nodeState) {
  const host = document.getElementById('opsStats');
  if (!host) return;
  const s = nodeState || {};
  const disk = s.disk || {};
  const pct = Math.min(100, Number(disk.used_pct) || 0);
  const corePct = s.core_budget ? (100 * (s.reserved_cores || 0) / s.core_budget) : 0;

  const counts = state.jobs.reduce((acc, j) => {
    const l = label(j);
    acc[l] = (acc[l] || 0) + 1;
    return acc;
  }, {});
  const terminal = ['Complete', 'Partial', 'Failed', 'Stopped']
    .map(k => [k, counts[k] || 0]).filter(p => p[1]);

  host.innerHTML = `
    <div class="card stat">
      <span class="stat-label">Cores reserved</span>
      <span class="stat-value">${s.reserved_cores ?? '—'}<small> / ${s.core_budget ?? '—'}</small></span>
      <div class="meter"><i style="width:${corePct.toFixed(1)}%"></i></div>
      <span class="small muted">${s.available_cores ?? '—'} free · ${s.running ?? 0} running</span>
    </div>
    <div class="card stat">
      <span class="stat-label">Queue</span>
      <span class="stat-value">${s.queued ?? 0}<small> waiting</small></span>
      <span class="small muted">${s.accepting === false ? 'Not accepting new jobs' : 'FIFO, no skip-ahead'}</span>
    </div>
    <div class="card stat">
      <span class="stat-label">Disk</span>
      <span class="stat-value">${fmtBytes(disk.used_bytes)}<small> / ${fmtBytes(disk.cap_bytes)}</small></span>
      <div class="meter ${pct > 90 ? 'bad' : pct > 70 ? 'warn' : 'ok'}"><i style="width:${pct}%"></i></div>
      <span class="small muted">${pct}% of the retention cap</span>
    </div>
    <div class="card stat">
      <span class="stat-label">Jobs on this node</span>
      <span class="stat-value">${state.jobs.length}</span>
      <div class="chips">${
        terminal.length
          ? terminal.map(([k, n]) => `<span class="badge ${k.toLowerCase()}">${n} ${k.toLowerCase()}</span>`).join('')
          : '<span class="small muted">none finished yet</span>'
      }</div>
    </div>`;
}

/* ------------------------------------------------------------------ table */

function renderFilters() {
  const host = document.getElementById('statusFilter');
  if (!host) return;
  host.innerHTML = FILTERS.map(f =>
    `<button class="chip" data-filter="${f}" aria-pressed="${f === state.filter}">${f}</button>`
  ).join('');
}

/* A sub-job is an ordinary job that happens to design one segment of a
   discontinuous peptide, so this label is the only thing in the table that
   distinguishes it. It carries the segment's own residue range because
   "sub-job 1" alone does not say WHICH piece of the peptide it designed, and
   that is the question someone comparing two rows actually has. */
function segLabel(j) {
  const s = j.segment;
  if (!s || !s.index) return '';
  const r = s.source_resseq;
  const rng = (Array.isArray(r) && r.length === 2)
    ? ` · resSeq ${esc(r[0])}–${esc(r[1])}` : '';
  return `<span class="sub-note">sub-job ${esc(s.index)} of ${esc(s.of)}${rng}</span>`;
}

function renderTable() {
  const body = document.getElementById('jobsBody');
  if (!body) return;
  const rows = visible();
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="8" class="empty">' +
      (state.jobs.length ? 'No jobs match this filter.' : 'No jobs on this node yet.') +
      '</td></tr>';
    return;
  }
  body.innerHTML = rows.map(j => {
    const sel = j.job_id === state.selected;
    const stoppable = j.status === 'Running' || j.status === 'Queued';
    return `<tr class="pick" data-id="${esc(j.job_id)}" aria-selected="${sel}">
      <td><strong>${esc(j.protein_symbol)}</strong><span class="sub-note mono">${esc(j.job_id)}</span>${segLabel(j)}</td>
      <td>${esc(j.user_name)}</td>
      <td class="mono">${esc(j.cpus)} · ${esc(j.peptide_length)}</td>
      <td>${badge(j)}${j.failed_stage ? `<span class="sub-note">${esc(j.failed_stage)}</span>` : ''}</td>
      <td>${iterCells(j)}</td>
      <td class="num">${j.best_score == null ? '—' : fmtNum(j.best_score)}</td>
      <td>${fmtElapsed(j.submitted_at, j.completed_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        ${stoppable ? `<button class="btn-sm" data-stop="${esc(j.job_id)}">Stop</button> ` : ''}
        <button class="btn-sm btn-danger" data-del="${esc(j.job_id)}">Delete</button>
      </td>
    </tr>`;
  }).join('');
}

/* ----------------------------------------------------------------- detail */

function stageBar(fraction, colour) {
  const pct = Math.round(Math.max(0, Math.min(1, fraction)) * 100);
  return 'height:4px;border-radius:2px;background:linear-gradient(90deg,' +
         colour + ' ' + pct + '%,#E4E7E2 ' + pct + '%)';
}

async function renderDetail() {
  const host = document.getElementById('jobDetail');
  if (!host) return;
  const job = state.jobs.find(j => j.job_id === state.selected);
  if (!job) {
    host.innerHTML = '<p class="empty" style="padding:8px 0">Select a job to see its progress.</p>';
    return;
  }

  const l = label(job);
  host.innerHTML = `
    <div style="display:flex;flex-wrap:wrap;justify-content:space-between;gap:10px;align-items:flex-start">
      <div style="display:flex;flex-direction:column;gap:4px">
        <span class="small muted">Selected job</span>
        <h2 class="mono" style="font-size:16px">${esc(job.job_id)}</h2>
        <span class="small">${esc(job.protein_symbol)} · ${esc(job.peptide_length)} residues ·
          ${esc(job.cpus)} cores · ${esc(job.user_name)}</span>
      </div>
      ${badge(job)}
    </div>
    ${job.message ? `<div class="alert ${job.partial ? 'alert-warn' : 'alert-ok'}" style="margin-top:12px">
      <strong>${esc(job.failed_stage || l)}</strong>${esc(job.message)}</div>` : ''}
    <div id="detailStages" style="margin-top:14px"></div>
    <div style="margin-top:14px">
      <span class="stat-label">Convergence</span>
      <div id="detailChart" style="margin-top:6px"></div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
      ${job.download_url ? `<a class="btn btn-sm" href="${esc(job.download_url)}" download>Download .zip</a>` : ''}
      ${job.status === 'Running' ? `<button class="btn-sm btn-danger" data-stop="${esc(job.job_id)}">Stop job</button>` : ''}
      ${['Complete', 'Partial'].includes(l) ? `<button class="btn-sm" data-open-results="${esc(job.job_id)}">Open in Results</button>` : ''}
    </div>`;

  // Per-iteration metrics: emitted by the manager as it goes. Absent on jobs
  // that ran before that was added, so its absence is reported, not hidden.
  let metrics = null;
  try { metrics = await api.metrics(job.job_id); } catch { /* older job */ }

  const stages = document.getElementById('detailStages');
  if (stages) {
    const cur = (metrics && metrics.current) || null;
    if (cur) {
      const defs = [
        ['Stage inputs', cur.inputs_done ? 1 : 0, (cur.parents || 0) + ' parents'],
        ['pepspec', (cur.waves_done || 0) / Math.max(1, cur.waves_total || 1),
          (cur.waves_done || 0) + ' / ' + (cur.waves_total || '?') + ' waves'],
        ['FoldX', (cur.foldx_done || 0) / Math.max(1, cur.foldx_total || 1),
          (cur.foldx_done || 0) + ' / ' + (cur.foldx_total || '?') + ' scored'],
        ['Select top 3', cur.selection_done ? 1 : 0, cur.selection_done ? 'done' : 'pending']
      ];
      stages.innerHTML =
        '<span class="stat-label">Iteration ' + esc(cur.iteration) + ' pipeline</span>' +
        '<ol style="list-style:none;margin:8px 0 0;padding:0;display:grid;' +
        'grid-template-columns:repeat(4,minmax(0,1fr));gap:6px">' +
        defs.map(([name, f, note]) =>
          '<li style="display:flex;flex-direction:column;gap:5px">' +
          '<span style="' + stageBar(f, f >= 1 ? '#1E7A4C' : '#2456C9') + '"></span>' +
          '<span class="small" style="font-weight:500">' + name + '</span>' +
          '<span class="small muted">' + esc(note) + '</span></li>').join('') +
        '</ol>';
    } else {
      stages.innerHTML = '<p class="chart-note">No per-iteration metrics for this job — ' +
        'the manager writes <span class="mono">iteration_metrics.json</span> only for runs ' +
        'started after that was added.</p>';
    }
  }

  const chartHost = document.getElementById('detailChart');
  if (chartHost) {
    const hist = (metrics && metrics.iterations) || [];
    if (hist.length) {
      renderLine(chartHost, {
        series: [
          { name: 'best', values: hist.map(h => h.best_score), color: '#2456C9' },
          { name: 'median', values: hist.map(h => h.median_score), color: '#9AA3AD', dashed: true }
        ],
        xLabels: hist.map(h => 'it' + h.iteration),
        label: 'Best and median FoldX energy by iteration for job ' + job.job_id
      });
    } else {
      chartHost.innerHTML = '<p class="chart-note">Needs per-iteration metrics.</p>';
    }
  }
}

/* -------------------------------------------------------------------- log */

async function refreshLog() {
  const pre = document.getElementById('logTail');
  const live = document.getElementById('logLive');
  if (!pre) return;
  if (!state.selected) {
    pre.textContent = '—';
    if (live) live.hidden = true;
    return;
  }
  const job = state.jobs.find(j => j.job_id === state.selected);
  try {
    const res = await api.log(state.selected, 8000);
    pre.textContent = (res && res.text) ? res.text.trimEnd() : '(log is empty)';
    pre.scrollTop = pre.scrollHeight;
    if (live) live.hidden = !(job && job.status === 'Running');
  } catch (e) {
    pre.textContent = 'Could not read the log: ' + e.message;
    if (live) live.hidden = true;
  }
}

/* ------------------------------------------------------------------- wire */

async function refresh() {
  const [jobsRes, nodeState] = await Promise.all([
    api.jobs().catch(e => ({ success: false, error: e.message })),
    api.state().catch(() => null)
  ]);

  if (jobsRes && jobsRes.success) {
    state.jobs = jobsRes.jobs || [];
    if (jobsRes.max_iterations) state.maxIterations = jobsRes.max_iterations;
    if (!state.selected) {
      const running = state.jobs.find(j => j.status === 'Running');
      state.selected = (running || state.jobs[0] || {}).job_id || null;
    }
  } else {
    const body = document.getElementById('jobsBody');
    if (body) {
      body.innerHTML = '<tr><td colspan="8" class="empty">Could not load jobs: ' +
        esc(jobsRes && jobsRes.error) + '</td></tr>';
    }
  }

  const stamp = document.getElementById('opsRefreshed');
  if (stamp) stamp.textContent = 'updated ' + new Date().toLocaleTimeString();

  if (nodeState) {
    const dot = document.getElementById('nodeDot');
    const name = document.getElementById('nodeName');
    const ver = document.getElementById('nodeVer');
    if (dot) dot.style.background = nodeState.accepting === false ? '#C5221F' : '#3CC17A';
    if (name) name.textContent = nodeState.accepting === false ? 'node · paused' : 'node · healthy';
    if (ver && nodeState.version) {
      ver.textContent = 'v' + nodeState.version +
        (nodeState.core_budget ? ' · ' + nodeState.core_budget + ' cores' : '');
    }
  }

  renderStats(nodeState);
  renderFilters();
  renderTable();
  await renderDetail();
}

export function init(ctx) {
  renderFilters();

  const search = document.getElementById('jobSearch');
  if (search) {
    search.addEventListener('input', () => {
      state.search = search.value;
      renderTable();
    });
  }

  document.getElementById('statusFilter')?.addEventListener('click', e => {
    const b = e.target.closest('[data-filter]');
    if (!b) return;
    state.filter = b.dataset.filter;
    renderFilters();
    renderTable();
  });

  document.getElementById('jobsBody')?.addEventListener('click', async e => {
    const stop = e.target.closest('[data-stop]');
    if (stop) {
      e.stopPropagation();
      if (!confirm('Stop job ' + stop.dataset.stop + '? Its pipeline is terminated.')) return;
      try { await api.stopJob(stop.dataset.stop); } catch (err) { alert(err.message); }
      return refresh();
    }
    const del = e.target.closest('[data-del]');
    if (del) {
      e.stopPropagation();
      if (!confirm('Delete ' + del.dataset.del + ' and all its files? This cannot be undone.')) return;
      try { await api.deleteJob(del.dataset.del); } catch (err) { alert(err.message); }
      if (state.selected === del.dataset.del) state.selected = null;
      return refresh();
    }
    const row = e.target.closest('tr[data-id]');
    if (row) {
      state.selected = row.dataset.id;
      renderTable();
      await renderDetail();
      await refreshLog();
    }
  });

  document.getElementById('jobDetail')?.addEventListener('click', async e => {
    const stop = e.target.closest('[data-stop]');
    if (stop) {
      if (!confirm('Stop job ' + stop.dataset.stop + '?')) return;
      try { await api.stopJob(stop.dataset.stop); } catch (err) { alert(err.message); }
      return refresh();
    }
    const open = e.target.closest('[data-open-results]');
    if (open && ctx && ctx.openResults) ctx.openResults(open.dataset.openResults);
  });

  document.querySelector('[data-goto="submit"]')
    ?.addEventListener('click', () => ctx && ctx.go && ctx.go('submit'));
}

export function show() {
  if (!state.stopPoll) state.stopPoll = poll(refresh, 5000);
  if (!state.stopLogPoll) state.stopLogPoll = poll(refreshLog, 4000);
}

export function hide() {
  if (state.stopPoll) { state.stopPoll(); state.stopPoll = null; }
  if (state.stopLogPoll) { state.stopLogPoll(); state.stopLogPoll = null; }
}

export function selectJob(id) { state.selected = id; }
