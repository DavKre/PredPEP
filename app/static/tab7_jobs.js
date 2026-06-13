// static/tab7_jobs.js — Jobs list tab (no module deps; attaches to window)
window.loadJobs = async function () {
    const tbody = document.getElementById('jobsTableBody');
    if (!tbody) return;
    try {
        const [jr, sr] = await Promise.all([fetch('/jobs'), fetch('/state')]);
        const data = await jr.json();
        const state = await sr.json().catch(() => null);
        renderDiskBar(state);
        if (!data.success) { tbody.innerHTML = `<tr><td colspan="8">Error: ${data.error || 'failed'}</td></tr>`; return; }
        if (!data.jobs.length) { tbody.innerHTML = `<tr><td colspan="8">No jobs yet.</td></tr>`; return; }
        const esc = s => String(s ?? '—').replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
        const cls = st => ({ Complete: 'status-complete', Running: 'status-processing', Queued: 'status-queued',
                             Stopped: 'status-stopped', Failed: 'status-stopped' }[st] || 'status-processing');
        tbody.innerHTML = data.jobs.map(j => {
            const date = j.submitted_at ? new Date(j.submitted_at).toLocaleString() : '—';
            const dl = j.download_url ? `<a href="${j.download_url}">Download</a>` : '—';
            const stoppable = (j.status === 'Running' || j.status === 'Queued');
            return `<tr>
                <td>${date}</td><td>${esc(j.protein_symbol)}</td><td>${esc(j.user_name)}</td>
                <td>${esc(j.cpus)}</td><td>${esc(j.peptide_length)}</td>
                <td class="${cls(j.status)}">${esc(j.status)}</td><td>${dl}</td>
                <td>${stoppable ? `<button class="job-stop" data-id="${encodeURIComponent(j.job_id)}">Stop</button> ` : ''}<button class="job-delete" data-id="${encodeURIComponent(j.job_id)}">Delete</button></td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('.job-stop').forEach(b =>
            b.addEventListener('click', () => window.stopJob(decodeURIComponent(b.dataset.id))));
        tbody.querySelectorAll('.job-delete').forEach(b =>
            b.addEventListener('click', () => window.deleteJob(decodeURIComponent(b.dataset.id))));
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8">Error loading jobs: ${e}</td></tr>`;
    }
};

function renderDiskBar(state) {
    const bar = document.getElementById('diskBar');
    const lbl = document.getElementById('diskLabel');
    if (!bar || !state || !state.disk) return;
    const gb = b => (b / (1024 ** 3)).toFixed(1);
    const pct = Math.min(100, state.disk.used_pct || 0);
    bar.style.width = pct + '%';
    bar.style.background = pct > 90 ? '#c5221f' : (pct > 70 ? '#b06000' : '#137333');
    lbl.textContent = `Disk: ${gb(state.disk.used_bytes)} / ${gb(state.disk.cap_bytes)} GB (${pct}%)  ·  Cores: ${state.reserved_cores}/${state.core_budget} used, ${state.queued} queued`;
}

window.stopJob = async function (jobId) {
    if (!confirm(`Stop job ${jobId}? Its pipeline will be terminated.`)) return;
    try {
        const res = await fetch(`/jobs/${encodeURIComponent(jobId)}/stop`, { method: 'POST' });
        const data = await res.json();
        if (!data.success) alert(`Stop failed: ${data.error || 'unknown'}`);
    } catch (e) { alert(`Stop error: ${e}`); }
    window.loadJobs();
};

window.deleteJob = async function (jobId) {
    if (!confirm(`Delete job ${jobId} and its files? This cannot be undone.`)) return;
    try {
        const res = await fetch(`/jobs/${encodeURIComponent(jobId)}`, { method: 'DELETE' });
        const data = await res.json();
        if (!data.success) alert(`Delete failed: ${data.error || 'unknown'}`);
    } catch (e) { alert(`Delete error: ${e}`); }
    window.loadJobs();
};

window.startJobsPolling = function () {
    window.loadJobs();
    if (window.jobsInterval) clearInterval(window.jobsInterval);
    window.jobsInterval = setInterval(window.loadJobs, 10000);
};
window.stopJobsPolling = function () {
    if (window.jobsInterval) { clearInterval(window.jobsInterval); window.jobsInterval = null; }
};
