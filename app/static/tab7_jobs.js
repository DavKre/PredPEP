// static/tab7_jobs.js — Jobs list tab (no module deps; attaches to window)
window.loadJobs = async function () {
    const tbody = document.getElementById('jobsTableBody');
    if (!tbody) return;
    try {
        const res = await fetch('/jobs');
        const data = await res.json();
        if (!data.success) { tbody.innerHTML = `<tr><td colspan="8">Error: ${data.error || 'failed'}</td></tr>`; return; }
        if (!data.jobs.length) { tbody.innerHTML = `<tr><td colspan="8">No jobs yet.</td></tr>`; return; }
        const esc = s => String(s ?? '—').replace(/[<>&]/g, c => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]));
        tbody.innerHTML = data.jobs.map(j => {
            const date = j.submitted_at ? new Date(j.submitted_at).toLocaleString() : '—';
            const dl = j.download_url ? `<a href="${j.download_url}">Download</a>` : '—';
            const cls = j.status === 'Complete' ? 'status-complete' : 'status-processing';
            return `<tr>
                <td>${date}</td><td>${esc(j.protein_symbol)}</td><td>${esc(j.user_name)}</td>
                <td>${esc(j.cpus)}</td><td>${esc(j.peptide_length)}</td>
                <td class="${cls}">${esc(j.status)}</td><td>${dl}</td>
                <td><button class="job-delete" data-id="${encodeURIComponent(j.job_id)}">Delete</button></td>
            </tr>`;
        }).join('');
        tbody.querySelectorAll('.job-delete').forEach(b =>
            b.addEventListener('click', () => window.deleteJob(decodeURIComponent(b.dataset.id))));
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="8">Error loading jobs: ${e}</td></tr>`;
    }
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
