/**
 * Jobs page — consolidated view of all MNM background tasks.
 */

async function checkAuth() {
  const resp = await fetch('/api/auth/check');
  const data = await resp.json();
  if (!data.authenticated) window.location.href = '/login';
}

function escHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function timeAgo(isoStr) {
  if (!isoStr) return '-';
  const diff = Date.now() - new Date(isoStr).getTime();
  if (diff < 0) return 'just now';
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return secs + 's ago';
  const mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  return Math.floor(hours / 24) + 'd ago';
}

function nextRun(lastRunIso, intervalSeconds) {
  if (!lastRunIso || !intervalSeconds) return '-';
  const next = new Date(lastRunIso).getTime() + intervalSeconds * 1000;
  const diff = next - Date.now();
  if (diff <= 0) return 'overdue';
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'imminent';
  if (mins < 60) return 'in ' + mins + 'm';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return 'in ' + hours + 'h';
  return 'in ' + Math.floor(hours / 24) + 'd';
}

function formatDuration(seconds) {
  if (seconds == null) return '-';
  if (seconds < 60) return Math.round(seconds) + 's';
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return m + 'm ' + s + 's';
}

function formatSchedule(job) {
  if (!job.schedule_interval) return '-';
  let label = 'every ' + job.schedule_interval;
  if (job.schedule_count && job.schedule_count > 1) {
    label += ' (' + job.schedule_count + ' schedules)';
  }
  return label;
}

function formatSummary(job) {
  const s = job.summary;
  if (!s) return '<span style="color:var(--text-muted)">-</span>';
  if (job.id === 'sweep') {
    return escHtml((s.alive || 0) + ' alive / ' + (s.total || 0) + ' scanned');
  }
  if (job.id === 'endpoint_collector') {
    return escHtml(
      (s.recorded || s.endpoints_recorded || 0) + ' recorded, '
      + (s.devices_queried || 0) + ' devices'
    );
  }
  if (job.id === 'proxmox_collector') {
    return escHtml((s.nodes || 0) + ' nodes, ' + (s.vms || 0) + ' VMs, ' + (s.containers || 0) + ' CTs');
  }
  if (job.id === 'db_prune') {
    const parts = Object.entries(s).filter(([, v]) => v > 0).map(([k, v]) => k + ': ' + v);
    return escHtml(parts.length ? parts.join(', ') : 'nothing pruned');
  }
  return escHtml(JSON.stringify(s));
}

// Keeps a reference so onclick can use it
let _jobs = [];

async function triggerJob(index) {
  const job = _jobs[index];
  if (!job || job.running || !job.enabled) return;

  const btn = document.querySelector(`[data-job-idx="${index}"]`);
  if (btn) { btn.disabled = true; btn.textContent = 'Starting...'; }

  try {
    const resp = await fetch(job.trigger_url, { method: 'POST' });
    if (resp.status === 409) {
      alert('Job is already running.');
      return;
    }
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      alert('Failed: ' + (err.detail || resp.statusText));
      return;
    }
    // Refresh immediately to pick up running state
    setTimeout(loadJobs, 500);
  } catch (e) {
    alert('Error: ' + e.message);
  }
}

async function loadJobs() {
  try {
    const resp = await fetch('/api/jobs');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    _jobs = data.jobs || [];

    const tbody = document.getElementById('jobs-tbody');
    if (!_jobs.length) {
      tbody.innerHTML = '<tr><td colspan="8" style="text-align:center;color:var(--text-muted)">No background jobs found.</td></tr>';
      return;
    }

    tbody.innerHTML = _jobs.map((job, i) => {
      const dotClass = !job.enabled ? 'disabled' : job.status;
      const statusLabel = !job.enabled ? 'disabled' : job.status;
      const resultCell = job.error
        ? '<span class="job-error">' + escHtml(String(job.error).substring(0, 200)) + '</span>'
        : '<span class="job-summary">' + formatSummary(job) + '</span>';
      const canRun = job.enabled && !job.running;

      return '<tr>'
        + '<td><span class="job-status"><span class="job-dot ' + dotClass + '"></span>' + statusLabel + '</span></td>'
        + '<td><strong>' + escHtml(job.name) + '</strong><div class="job-name-sub">' + escHtml(job.description) + '</div></td>'
        + '<td>' + formatSchedule(job) + '</td>'
        + '<td title="' + escHtml(job.last_run || '') + '">' + timeAgo(job.last_run) + '</td>'
        + '<td>' + (job.running ? '<em>running</em>' : nextRun(job.last_run, job.schedule_seconds)) + '</td>'
        + '<td>' + formatDuration(job.duration_seconds) + '</td>'
        + '<td>' + resultCell + '</td>'
        + '<td><button class="btn small" data-job-idx="' + i + '" onclick="triggerJob(' + i + ')" '
        +   (canRun ? '' : 'disabled') + '>'
        +   (job.running ? 'Running...' : 'Run Now')
        + '</button></td>'
        + '</tr>';
    }).join('');

    document.getElementById('last-refresh').textContent = 'Updated ' + new Date().toLocaleTimeString();
  } catch (e) {
    console.error('Failed to load jobs:', e);
  }
}

// Expose for onclick
window.triggerJob = triggerJob;

checkAuth();
loadJobs();
setInterval(loadJobs, 10000);
