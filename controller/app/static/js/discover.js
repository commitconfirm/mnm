/* MNM Discovery page logic — enriched sweep UI */

let pollInterval = null;
let showDead = false;
const expandedIPs = new Set();

async function checkAuth() {
  const resp = await fetch('/api/auth/check');
  const data = await resp.json();
  if (!data.authenticated) window.location.href = '/login';
}

async function loadOptions() {
  try {
    const locResp = await fetch('/api/nautobot/locations');
    if (locResp.status === 401) { window.location.href = '/login'; return; }
    const locData = await locResp.json();
    const locSelect = document.getElementById('location-select');
    locSelect.innerHTML = (locData.locations || [])
      .map(l => `<option value="${l.id}">${l.display}</option>`)
      .join('');

    const credResp = await fetch('/api/nautobot/secrets-groups');
    const credData = await credResp.json();
    const credSelect = document.getElementById('creds-select');
    credSelect.innerHTML = (credData.secrets_groups || [])
      .map(g => `<option value="${g.id}">${g.name}</option>`)
      .join('');

    document.getElementById('start-btn').disabled = false;
  } catch (e) {
    console.error('Failed to load options:', e);
  }
}

async function loadSchedule() {
  try {
    const resp = await fetch('/api/discover/schedule');
    if (!resp.ok) return;
    const data = await resp.json();
    const schedules = data.schedules || [];
    const statusEl = document.getElementById('schedule-status');
    const infoEl = document.getElementById('schedule-info');

    if (schedules.length > 0) {
      const s = schedules[0];
      const parts = [`Scheduled: ${formatInterval(s.interval_hours)}`];
      if (s.last_run) parts.push(`Last: ${timeAgo(s.last_run)}`);
      const ranges = (s.cidr_ranges || []).join(', ');
      if (ranges) parts.push(`Ranges: ${ranges}`);
      infoEl.textContent = parts.join(' | ');
      statusEl.style.display = 'block';
    } else {
      statusEl.style.display = 'none';
    }
  } catch (e) {
    console.error('Failed to load schedule:', e);
  }
}

function timeAgo(isoStr) {
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.floor(hours / 24);
  return days + 'd ago';
}

function timeUntil(isoStr) {
  const diff = new Date(isoStr).getTime() - Date.now();
  if (diff < 0) return 'overdue';
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'imminent';
  if (mins < 60) return 'in ' + mins + 'm';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return 'in ' + hours + 'h';
  const days = Math.floor(hours / 24);
  return 'in ' + days + 'd';
}

function parseInterval(mode) {
  const map = { '1h': 1, '4h': 4, '12h': 12, '24h': 24, '7d': 168 };
  return map[mode] || 24;
}

function formatInterval(hours) {
  if (hours >= 168) return 'every 7 days';
  if (hours >= 24) return `every ${Math.round(hours / 24)} day(s)`;
  return `every ${hours} hour(s)`;
}

async function clearSchedule() {
  try {
    const config = await (await fetch('/api/config')).json();
    config.sweep_schedules = [];
    await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(config),
    });
    loadSchedule();
  } catch (e) {
    console.error('Failed to clear schedule:', e);
  }
}

async function startSweep() {
  const cidrText = document.getElementById('cidr-input').value.trim();
  const cidrRanges = cidrText.split('\n').map(s => s.trim()).filter(Boolean);
  const locationId = document.getElementById('location-select').value;
  const secretsGroupId = document.getElementById('creds-select').value;
  const snmpCommunity = document.getElementById('snmp-community').value.trim() || 'public';
  const runMode = document.getElementById('run-mode').value;

  if (!cidrRanges.length || !locationId || !secretsGroupId) {
    alert('Please fill in all fields');
    return;
  }

  // If a recurring schedule is selected, save it
  if (runMode !== 'immediately') {
    try {
      await fetch('/api/discover/schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          cidr_ranges: cidrRanges,
          location_id: locationId,
          secrets_group_id: secretsGroupId,
          interval_hours: parseInterval(runMode),
          snmp_community: snmpCommunity,
        }),
      });
      loadSchedule();
    } catch (e) {
      console.error('Failed to save schedule:', e);
    }
  }

  // Always run immediately too
  document.getElementById('start-btn').disabled = true;
  document.getElementById('start-btn').textContent = 'Sweep Running...';
  document.getElementById('stop-btn').style.display = 'inline-flex';
  document.getElementById('stop-btn').disabled = false;
  document.getElementById('stop-btn').textContent = 'Stop Sweep';
  document.getElementById('progress-header-card').style.display = 'block';
  document.getElementById('progress-card').style.display = 'block';
  document.getElementById('summary-section').style.display = 'block';

  try {
    const resp = await fetch('/api/discover/sweep', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        cidr_ranges: cidrRanges,
        location_id: locationId,
        secrets_group_id: secretsGroupId,
        snmp_community: snmpCommunity,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      alert(err.detail || 'Failed to start sweep');
      document.getElementById('start-btn').disabled = false;
      document.getElementById('start-btn').textContent = 'Run Sweep';
      return;
    }

    pollInterval = setInterval(pollStatus, 2000);
  } catch (e) {
    alert('Failed to start sweep: ' + e.message);
    document.getElementById('start-btn').disabled = false;
  }
}

function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function classificationBadge(cls) {
  if (!cls) return '';
  const label = cls.replace(/_/g, ' ');
  return `<span class="badge ${cls}">${label}</span>`;
}

function statusBadge(status) {
  return `<span class="badge ${status}">${status}</span>`;
}

function onboardingBlockHtml(state) {
  if (!state) return '';
  const stage = (state.stage || '').toLowerCase();
  const stageColor = ({
    submitting: '#888',
    queued: '#888',
    running: '#0a73c0',
    succeeded: '#1c8a3a',
    failed: '#b71c1c',
    timeout: '#b71c1c',
    none: '#888',
  })[stage] || '#888';
  const errBlock = state.error
    ? `<div class="field"><div class="field-label">Error Detail</div><div class="field-value" style="white-space:pre-wrap;color:#b71c1c;">${escHtml(state.error)}</div></div>`
    : '';
  const jrLink = state.job_result_id
    ? `<div class="field"><div class="field-label">Nautobot Job Result</div><div class="field-value"><a target="_blank" href="/job-results/${escHtml(state.job_result_id)}/">${escHtml(state.job_result_id)}</a></div></div>`
    : '';
  return `
    <div class="field" style="grid-column: 1 / -1; border-left: 4px solid ${stageColor}; padding-left: 8px; background: #f7f7f7;">
      <div class="field-label">Onboarding Status</div>
      <div class="field-value"><strong style="color:${stageColor};text-transform:uppercase;">${escHtml(stage)}</strong> &mdash; ${escHtml(state.message || '')}</div>
    </div>
    ${jrLink}
    ${errBlock}
  `;
}

function renderTable(hosts, onboardingByIp) {
  onboardingByIp = onboardingByIp || {};
  const tbody = document.getElementById('sweep-table');
  const entries = Object.entries(hosts);

  const rows = [];
  for (const [ip, info] of entries) {
    // Hide incomplete hosts (pending/scanning/enriching) — they appear once fully processed
    if (['pending', 'scanning', 'enriching'].includes(info.status)) continue;
    if (!showDead && info.status === 'dead') continue;

    const ports = (info.ports_open || []).join(', ') || '-';
    const nautobotUrl = info.nautobot_url || '';
    const actionParts = [];

    if (nautobotUrl) {
      actionParts.push(`<a href="${escHtml(nautobotUrl)}" target="_blank" class="btn small">View</a>`);
    }
    actionParts.push(`<button class="btn small detail-toggle" data-ip="${escHtml(ip)}">Details</button>`);

    // Build SSH/HTTP hint column (truncated banner or title)
    const sshHttp = info.ssh_banner
      ? escHtml(info.ssh_banner.substring(0, 30))
      : info.http_title
        ? escHtml(info.http_title.substring(0, 30))
        : '-';

    rows.push(`<tr data-ip="${escHtml(ip)}">
      <td>${escHtml(ip)}</td>
      <td>${escHtml(info.dns_name || '-')}</td>
      <td>${escHtml(info.mac_vendor || '-')}</td>
      <td>${escHtml(ports)}</td>
      <td>${escHtml((info.snmp && info.snmp.sysName || '') || '-')}</td>
      <td>${sshHttp}</td>
      <td>${classificationBadge(info.classification)}</td>
      <td>${statusBadge(info.status)}</td>
      <td>${actionParts.join(' ')}</td>
    </tr>`);

    // Detail row (preserve expanded state across re-renders)
    const isExpanded = expandedIPs.has(ip);
    rows.push(`<tr class="detail-row" id="detail-${escHtml(ip)}" style="display: ${isExpanded ? 'table-row' : 'none'};">
      <td colspan="9">
        <div class="detail-content">
          ${onboardingBlockHtml(onboardingByIp[ip])}
          <div class="field">
            <div class="field-label">MAC Address</div>
            <div class="field-value">${escHtml(info.mac_address || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">MAC Vendor</div>
            <div class="field-value">${escHtml(info.mac_vendor || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">SNMP sysName</div>
            <div class="field-value">${escHtml((info.snmp && info.snmp.sysName || '') || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">SNMP sysDescr</div>
            <div class="field-value">${escHtml((info.snmp && info.snmp.sysDescr || '') || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">SNMP sysObjectID</div>
            <div class="field-value">${escHtml((info.snmp && info.snmp.sysObjectID || '') || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">SNMP sysContact</div>
            <div class="field-value">${escHtml((info.snmp && info.snmp.sysContact || '') || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">SNMP sysLocation</div>
            <div class="field-value">${escHtml((info.snmp && info.snmp.sysLocation || '') || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">Open Ports</div>
            <div class="field-value">${escHtml((info.ports_open || []).join(', ') || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">First Seen</div>
            <div class="field-value">${escHtml(info.first_seen || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">Last Seen</div>
            <div class="field-value">${escHtml(info.last_seen || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">Classification</div>
            <div class="field-value">${escHtml(info.classification || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">DNS Name</div>
            <div class="field-value">${escHtml(info.dns_name || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">SSH Banner</div>
            <div class="field-value">${escHtml(info.ssh_banner || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">HTTP Title</div>
            <div class="field-value">${escHtml(info.http_title || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">TLS Subject</div>
            <div class="field-value">${escHtml(info.tls_subject || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">TLS Issuer</div>
            <div class="field-value">${escHtml(info.tls_issuer || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">TLS Expiry</div>
            <div class="field-value">${escHtml(info.tls_expiry || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">TLS SANs</div>
            <div class="field-value">${escHtml(info.tls_sans || '-')}</div>
          </div>
          <div class="field">
            <div class="field-label">Service Banners</div>
            <div class="field-value">${escHtml(JSON.stringify(info.banners || {}) === '{}' ? '-' : JSON.stringify(info.banners, null, 1))}</div>
          </div>
          <div class="field">
            <div class="field-label">HTTP Headers</div>
            <div class="field-value">${escHtml(JSON.stringify(info.http_headers || {}) === '{}' ? '-' : JSON.stringify(info.http_headers, null, 1))}</div>
          </div>
        </div>
      </td>
    </tr>`);
  }

  tbody.innerHTML = rows.join('');

  // Attach detail toggle handlers
  tbody.querySelectorAll('.detail-toggle').forEach(btn => {
    const ip = btn.getAttribute('data-ip');
    // Restore button text if expanded
    if (expandedIPs.has(ip)) btn.textContent = 'Hide';

    btn.addEventListener('click', () => {
      const detailRow = document.getElementById('detail-' + ip);
      if (detailRow) {
        const visible = detailRow.style.display !== 'none';
        detailRow.style.display = visible ? 'none' : 'table-row';
        btn.textContent = visible ? 'Details' : 'Hide';
        if (visible) {
          expandedIPs.delete(ip);
        } else {
          expandedIPs.add(ip);
        }
      }
    });
  });
}

function updateStats(hosts) {
  const all = Object.values(hosts);
  const alive = all.filter(h => h.status !== 'dead');

  document.getElementById('stat-scanned').textContent = all.length;
  document.getElementById('stat-alive').textContent = alive.length;
  document.getElementById('stat-network').textContent =
    all.filter(h => h.classification === 'network_device').length;
  document.getElementById('stat-servers').textContent =
    all.filter(h => h.classification === 'server').length;
  document.getElementById('stat-aps').textContent =
    all.filter(h => h.classification === 'access_point').length;
  document.getElementById('stat-endpoints').textContent =
    all.filter(h => h.classification === 'endpoint').length;
  document.getElementById('stat-onboarded').textContent =
    all.filter(h => h.status === 'onboarded').length;
  document.getElementById('stat-recorded').textContent =
    all.filter(h => h.status === 'recorded').length;
}

async function pollStatus() {
  try {
    const resp = await fetch('/api/discover/status');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    const hosts = data.hosts || {};

    if (Object.keys(hosts).length > 0) {
      document.getElementById('progress-card').style.display = 'block';
      document.getElementById('summary-section').style.display = 'block';
    }

    // Progress header card (bar + log)
    const headerCard = document.getElementById('progress-header-card');
    const total = Object.keys(hosts).length;
    const done = Object.values(hosts).filter(h => h.status !== 'pending' && h.status !== 'scanning').length;
    const progressBar = document.getElementById('progress-bar');
    const progressLabel = document.getElementById('progress-label');
    const progressCount = document.getElementById('progress-count');
    const indicator = document.getElementById('scan-indicator');

    if (total > 0) {
      headerCard.style.display = 'block';
      const pct = Math.round((done / total) * 100);
      progressBar.style.width = pct + '%';
      progressCount.textContent = `${done} / ${total}`;

      if (data.running) {
        progressBar.classList.remove('complete');
        progressLabel.textContent = 'Scanning...';
        indicator.style.display = 'inline';
        indicator.textContent = `${pct}%`;
      } else {
        progressBar.classList.add('complete');
        progressBar.style.width = '100%';
        progressLabel.textContent = 'Sweep complete';
        indicator.style.display = 'none';
      }
    }

    // Scan log
    const logEl = document.getElementById('scan-log');
    if (data.log && data.log.length > 0) {
      logEl.textContent = data.log.join('\n');
      logEl.scrollTop = logEl.scrollHeight;
    }

    const onboardingByIp = {};
    for (const o of (data.onboarding || [])) {
      if (o && o.ip) onboardingByIp[o.ip] = o;
    }
    renderTable(hosts, onboardingByIp);
    updateStats(hosts);

    // Stop polling when done
    if (!data.running && pollInterval) {
      clearInterval(pollInterval);
      pollInterval = null;
      document.getElementById('start-btn').disabled = false;
      document.getElementById('start-btn').textContent = 'Run Sweep';
      document.getElementById('stop-btn').style.display = 'none';
      loadHistory();
    }
    // Keep button disabled while running
    if (data.running) {
      document.getElementById('start-btn').disabled = true;
      document.getElementById('start-btn').textContent = 'Sweep Running...';
    }
  } catch (e) {
    console.error('Poll failed:', e);
  }
}

// Event listeners
document.getElementById('start-btn').addEventListener('click', startSweep);

document.getElementById('stop-btn').addEventListener('click', async () => {
  try {
    await fetch('/api/discover/stop', { method: 'POST' });
    document.getElementById('stop-btn').disabled = true;
    document.getElementById('stop-btn').textContent = 'Stopping...';
  } catch (e) {
    console.error('Failed to stop sweep:', e);
  }
});
document.getElementById('clear-schedule-btn').addEventListener('click', clearSchedule);

document.getElementById('show-dead-toggle').addEventListener('change', (e) => {
  showDead = e.target.checked;
  pollStatus();
});

document.getElementById('show-log-toggle').addEventListener('change', (e) => {
  document.getElementById('scan-log').style.display = e.target.checked ? 'block' : 'none';
});

document.getElementById('logout-link').addEventListener('click', async (e) => {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
});

async function loadHistory() {
  try {
    const resp = await fetch('/api/discover/history');
    if (!resp.ok) return;
    const data = await resp.json();
    const history = data.history || [];
    const tbody = document.getElementById('history-table');

    if (history.length === 0) {
      tbody.innerHTML = '<tr><td colspan="7" style="color: var(--text-muted); text-align: center;">No sweep history yet</td></tr>';
      return;
    }

    tbody.innerHTML = history.map(h => {
      const s = h.summary || {};
      const ranges = (h.cidr_ranges || []).join(', ');
      const started = h.started_at ? new Date(h.started_at).toLocaleString() : '-';
      const dur = h.duration_seconds != null ? h.duration_seconds + 's' : '-';
      return `<tr>
        <td>${started}</td>
        <td>${dur}</td>
        <td>${escHtml(ranges)}</td>
        <td>${s.alive || 0}</td>
        <td>${s.onboarded || 0}</td>
        <td>${s.recorded || 0}</td>
        <td>${s.failed || 0}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.error('Failed to load history:', e);
  }
}

// Theme switcher
function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('mnm-theme', theme);
  document.querySelectorAll('.theme-switcher button').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-theme') === theme);
  });
}
(function initTheme() {
  const saved = localStorage.getItem('mnm-theme') || 'dark';
  setTheme(saved);
  document.querySelectorAll('.theme-switcher button').forEach(btn => {
    btn.addEventListener('click', () => setTheme(btn.getAttribute('data-theme')));
  });
})();

// Init preferences
MNMPreferences.init();

// Init
// ---------------------------------------------------------------------------
// Discovery exclusions (Rule 6 — operator-controlled scope)
// ---------------------------------------------------------------------------
async function loadExcludes() {
  try {
    const r = await fetch('/api/discover/excludes');
    if (!r.ok) return;
    const data = await r.json();
    const tbody = document.getElementById('excludes-tbody');
    if (!tbody) return;
    const rows = data.excludes || [];
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="6" style="color:var(--text-muted); text-align:center">No exclusions</td></tr>';
      return;
    }
    const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; };
    tbody.innerHTML = rows.map(e => {
      const typeBadge = e.type === 'ip'
        ? '<span class="badge scanning">IP</span>'
        : '<span class="badge known">Device</span>';
      return '<tr>' +
        '<td><code>' + esc(e.identifier) + '</code></td>' +
        '<td>' + typeBadge + '</td>' +
        '<td>' + esc(e.reason) + '</td>' +
        '<td>' + esc(e.created_by) + '</td>' +
        '<td>' + esc(e.created_at ? new Date(e.created_at).toLocaleString() : '') + '</td>' +
        '<td><button class="btn exclude-remove" data-identifier="' + esc(e.identifier) + '">Remove</button></td>' +
      '</tr>';
    }).join('');
    document.querySelectorAll('.exclude-remove').forEach(btn => {
      btn.addEventListener('click', async () => {
        const ident = btn.getAttribute('data-identifier');
        if (!confirm('Remove exclusion for ' + ident + '?')) return;
        await fetch('/api/discover/excludes/' + encodeURIComponent(ident), { method: 'DELETE' });
        loadExcludes();
      });
    });
  } catch (e) { console.error('Failed to load excludes:', e); }
}

document.getElementById('exclude-add-btn').addEventListener('click', async () => {
  const ip = document.getElementById('exclude-ip').value.trim();
  const name = document.getElementById('exclude-name').value.trim();
  const reason = document.getElementById('exclude-reason').value.trim();
  if (!ip && !name) {
    alert('Enter an IP address OR a device name to exclude.');
    return;
  }
  // If both are provided, the IP wins (sweep skip is the higher-impact action).
  // To exclude both, the operator submits twice.
  const body = ip
    ? { identifier: ip, type: 'ip', reason }
    : { identifier: name, type: 'device_name', reason };
  const r = await fetch('/api/discover/excludes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (r.ok) {
    document.getElementById('exclude-ip').value = '';
    document.getElementById('exclude-name').value = '';
    document.getElementById('exclude-reason').value = '';
    loadExcludes();
  } else {
    const err = await r.json().catch(() => ({}));
    alert('Failed to add exclusion: ' + (err.detail || r.status));
  }
});

checkAuth().then(() => {
  loadOptions();
  loadSchedule();
  loadHistory();
  loadExcludes();
  // Check if there's an active sweep — resume polling and disable start button
  fetch('/api/discover/status').then(r => r.json()).then(data => {
    if (data.running || Object.keys(data.hosts || {}).length > 0) {
      document.getElementById('progress-header-card').style.display = 'block';
      document.getElementById('progress-card').style.display = 'block';
      document.getElementById('summary-section').style.display = 'block';
      pollStatus();
      if (data.running) {
        document.getElementById('start-btn').disabled = true;
        document.getElementById('start-btn').textContent = 'Sweep Running...';
        document.getElementById('stop-btn').style.display = 'inline-flex';
        pollInterval = setInterval(pollStatus, 2000);
      }
    }
  }).catch(() => {});
});
