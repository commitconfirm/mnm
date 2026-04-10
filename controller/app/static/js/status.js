/* MNM Dashboard — status page logic */

async function checkAuth() {
  const resp = await fetch('/api/auth/check');
  const data = await resp.json();
  if (!data.authenticated) {
    window.location.href = '/login';
  }
}

function statusDot(health, status, name) {
  const svc = name || '';
  let color, msg;
  if (health === 'healthy') { color = 'green'; msg = 'Healthy and running'; }
  else if (health === 'unhealthy') { color = 'red'; msg = 'Unhealthy — check logs'; }
  else if (health === 'starting') { color = 'yellow'; msg = 'Starting — waiting for health check'; }
  else if (status === 'running') { color = 'yellow'; msg = 'Running — no health check'; }
  else if (status === 'exited') { color = 'red'; msg = 'Exited — container stopped'; }
  else if (status === 'restarting') { color = 'yellow'; msg = 'Restarting'; }
  else { color = 'grey'; msg = status || 'Unknown'; }
  return `<span class="dot-wrap"><span class="dot ${color}"></span><span class="dot-tooltip">${svc}: ${msg}</span></span>`;
}

async function loadContainers() {
  try {
    const resp = await fetch('/api/status');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    const containers = data.containers || [];

    document.getElementById('container-count').textContent = containers.length;
    document.getElementById('healthy-count').textContent =
      containers.filter(c => c.health === 'healthy').length;

    const tbody = document.getElementById('container-table');
    tbody.innerHTML = containers.map(c => `
      <tr>
        <td class="center">${statusDot(c.health, c.status, c.name)}</td>
        <td>${c.name}</td>
        <td>${c.image}</td>
        <td>${c.health}</td>
        <td>${c.ports.join(', ') || '-'}</td>
      </tr>
    `).join('');
  } catch (e) {
    console.error('Failed to load containers:', e);
  }
}

async function loadDevices() {
  try {
    const resp = await fetch('/api/nautobot/devices');
    if (resp.status === 401) return;
    const data = await resp.json();
    document.getElementById('device-count').textContent = (data.devices || []).length;
  } catch (e) {
    document.getElementById('device-count').textContent = '?';
  }
}

async function loadIPCount() {
  try {
    const resp = await fetch('/api/nautobot/ip-count');
    if (resp.status === 401) return;
    const data = await resp.json();
    document.getElementById('ip-count').textContent = data.count != null ? data.count : '-';
  } catch (e) {
    document.getElementById('ip-count').textContent = '?';
  }
}

async function loadNeighbors() {
  try {
    const resp = await fetch('/api/discover/neighbors');
    if (resp.status === 401) return;
    const data = await resp.json();
    const neighbors = data.neighbors || [];

    const card = document.getElementById('neighbors-card');
    if (neighbors.length === 0) {
      card.style.display = 'none';
      return;
    }

    card.style.display = 'block';
    document.getElementById('neighbor-alert').textContent =
      `${neighbors.length} unrecognized neighbor(s) detected on your network`;

    const tbody = document.getElementById('neighbor-table');
    tbody.innerHTML = neighbors.map(n => `
      <tr>
        <td>${escHtml(n.neighbor_name)}</td>
        <td>${escHtml(n.connected_to)}</td>
        <td>${n.classification ? `<span class="badge ${n.classification}">${n.classification.replace(/_/g, ' ')}</span>` : '-'}</td>
        <td>${escHtml(n.mac_vendor || '-')}</td>
      </tr>
    `).join('');
  } catch (e) {
    console.error('Failed to load neighbors:', e);
  }
}

function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
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

async function loadSweepInfo() {
  try {
    const resp = await fetch('/api/discover/schedule');
    if (!resp.ok) return;
    const data = await resp.json();
    const card = document.getElementById('sweep-info-card');
    const summary = document.getElementById('sweep-summary');

    const parts = [];
    if (data.last_run) {
      parts.push('Last sweep: ' + timeAgo(data.last_run));
    }
    if (data.interval) {
      parts.push('Schedule: every ' + data.interval);
    }

    if (parts.length > 0) {
      card.style.display = 'block';
      summary.textContent = parts.join(' | ');
    }
  } catch (e) {
    // Sweep info is optional
  }
}

let collectionPollInterval = null;
// Track whether the current poll cycle has actually observed running=true.
// Without this, the 2s poll can fire before collect_all() flips the flag,
// see running=false, and prematurely declare the run "done".
let collectionRunObserved = false;

async function loadEndpointSummary() {
  try {
    const resp = await fetch('/api/endpoints/summary');
    if (resp.status === 401) return;
    const data = await resp.json();
    document.getElementById('endpoint-count').textContent = data.total_endpoints != null ? data.total_endpoints : '-';
    document.getElementById('endpoint-vlans').textContent = data.vlans_active != null ? data.vlans_active : '-';
    document.getElementById('endpoint-last').textContent = data.last_collection ? timeAgo(data.last_collection) : '-';

    // If collection is running, start polling progress
    if (data.running && !collectionPollInterval) {
      collectionPollInterval = setInterval(pollCollectionProgress, 2000);
      document.getElementById('trigger-collection-btn').disabled = true;
      document.getElementById('trigger-collection-btn').textContent = 'Collecting...';
    }
  } catch (e) {
    console.error('Failed to load endpoint summary:', e);
  }
}

async function pollCollectionProgress() {
  try {
    const resp = await fetch('/api/endpoints/collection-status');
    if (resp.status === 401) return;
    const data = await resp.json();
    const p = data.progress || {};
    const progressDiv = document.getElementById('collection-progress');
    const bar = document.getElementById('collection-bar');
    const phaseEl = document.getElementById('collection-phase');
    const countEl = document.getElementById('collection-count');

    if (data.running) {
      collectionRunObserved = true;
      progressDiv.style.display = 'block';
      const total = p.devices_total || 1;
      const done = p.devices_done || 0;
      const pct = Math.round((done / total) * 100);
      bar.style.width = pct + '%';
      bar.classList.remove('complete');
      phaseEl.textContent = p.phase === 'recording' ? 'Recording to Nautobot...' : `Collecting from devices...`;
      countEl.textContent = `${done} / ${total} devices (${p.endpoints_found || 0} endpoints)`;
    } else if (!collectionRunObserved) {
      // Run hasn't actually started yet (POST returned before collect_all
      // flipped the running flag). Keep polling, don't reset the button.
      return;
    } else {
      // Done
      collectionRunObserved = false;
      bar.style.width = '100%';
      bar.classList.add('complete');
      phaseEl.textContent = 'Collection complete';
      countEl.textContent = `${p.endpoints_found || 0} endpoints`;
      document.getElementById('trigger-collection-btn').disabled = false;
      document.getElementById('trigger-collection-btn').textContent = 'Trigger Collection';
      if (collectionPollInterval) {
        clearInterval(collectionPollInterval);
        collectionPollInterval = null;
      }
      loadEndpointSummary();
      // Hide progress bar after a few seconds
      setTimeout(() => { progressDiv.style.display = 'none'; }, 5000);
    }
  } catch (e) {
    console.error('Failed to poll collection progress:', e);
  }
}

// Service links must point at the same hostname the operator used to reach
// MNM (so SSH-tunneled / Tailscale / LAN-IP access all work). Run on script
// parse — before any user interaction — so the rendered hrefs are never the
// placeholder hashes from the HTML.
//
// Grafana goes through Traefik on :8080/grafana/ rather than direct :3000
// because direct port access bounces through GF_SERVER_ROOT_URL and the
// container's configured root URL is the subpath behind Traefik — direct
// access lands on a redirect to a path the proxy isn't routing on :3000.
function fixServiceLinks() {
  const host = window.location.hostname;
  const proto = window.location.protocol;
  const urlMap = {
    'Nautobot':   `${proto}//${host}:8443`,
    'Grafana':    `${proto}//${host}:8080/grafana/`,
    'Prometheus': `${proto}//${host}:8080/prometheus/`,
  };
  document.querySelectorAll('.service-link').forEach(link => {
    const name = link.textContent.trim();
    if (urlMap[name]) link.href = urlMap[name];
  });
}
fixServiceLinks();

document.getElementById('logout-link').addEventListener('click', async (e) => {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
});

// Sync Network Data button
document.getElementById('sync-all-btn').addEventListener('click', async () => {
  const btn = document.getElementById('sync-all-btn');
  const status = document.getElementById('sync-status');
  btn.disabled = true;
  btn.textContent = 'Syncing...';
  status.style.display = 'block';
  status.textContent = 'Submitting sync job for all onboarded devices...';
  try {
    const resp = await fetch('/api/nautobot/sync-network-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sync_cables: true }),
    });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    if (resp.ok) {
      const jobId = data.job_result && data.job_result.id ? data.job_result.id : '';
      status.textContent = 'Sync job submitted. Check Nautobot Jobs for progress.' + (jobId ? ' Job ID: ' + jobId.substring(0, 8) + '...' : '');
    } else {
      status.textContent = 'Sync failed: ' + (data.detail || 'Unknown error');
    }
  } catch (e) {
    status.textContent = 'Sync failed: ' + e.message;
  }
  btn.textContent = 'Sync Network Data';
  btn.disabled = false;
  setTimeout(() => { status.style.display = 'none'; }, 15000);
});

document.getElementById('trigger-collection-btn').addEventListener('click', async () => {
  const btn = document.getElementById('trigger-collection-btn');
  btn.disabled = true;
  btn.textContent = 'Collecting...';
  collectionRunObserved = false;
  document.getElementById('collection-progress').style.display = 'block';
  try {
    const resp = await fetch('/api/endpoints/collect', { method: 'POST' });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    if (resp.status === 409) {
      // Already running — just start polling the existing run
      btn.textContent = 'Collection in progress...';
    }
    if (!collectionPollInterval) {
      collectionPollInterval = setInterval(pollCollectionProgress, 2000);
    }
  } catch (e) {
    console.error('Failed to trigger collection:', e);
    btn.textContent = 'Trigger Collection';
    btn.disabled = false;
  }
});

// Theme switcher
function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('mnm-theme', theme);
  document.querySelectorAll('.theme-switcher button').forEach(btn => {
    btn.classList.toggle('active', btn.getAttribute('data-theme') === theme);
  });
}

function initTheme() {
  const saved = localStorage.getItem('mnm-theme') || 'dark';
  setTheme(saved);
  document.querySelectorAll('.theme-switcher button').forEach(btn => {
    btn.addEventListener('click', () => setTheme(btn.getAttribute('data-theme')));
  });
}

// Init
initTheme();
MNMPreferences.init();

let containerRefreshTimer = null;

function startAutoRefresh() {
  if (containerRefreshTimer) { clearInterval(containerRefreshTimer); containerRefreshTimer = null; }
  const interval = MNMPreferences.get('autoRefreshInterval');
  if (interval && interval > 0) {
    containerRefreshTimer = setInterval(function() {
      loadContainers();
      loadPolling();
    }, interval * 1000);
  }
}

window.addEventListener('mnm-preferences-changed', () => {
  startAutoRefresh();
});

async function loadRecentEvents() {
  try {
    const r = await fetch('/api/endpoints/events?since=24h&limit=10');
    if (!r.ok) return;
    const data = await r.json();
    const tbody = document.getElementById('recent-events-tbody');
    if (!tbody) return;
    if (!data.events || !data.events.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">No events in the last 24 hours</td></tr>';
      return;
    }
    const esc = (s) => { const d = document.createElement('div'); d.textContent = s == null ? '' : String(s); return d.innerHTML; };
    tbody.innerHTML = data.events.slice(0, 10).map(e => {
      const when = new Date(e.timestamp).toLocaleString();
      const watched = e.details && e.details.watched === true;
      const star = watched ? ' <span title="Watched">👁</span>' : '';
      const rowStyle = watched ? ' style="background:rgba(255,200,0,0.08)"' : '';
      const macLink = '<a href="/endpoints/' + encodeURIComponent(e.mac_address) + '"><code>' + esc(e.mac_address) + '</code></a>' + star;
      const fromTo = (e.old_value || '-') + ' \u2192 ' + (e.new_value || '-');
      return '<tr' + rowStyle + '><td>' + esc(when) + '</td><td>' + macLink + '</td><td><span class="badge">' + esc(e.event_type) + '</span></td><td>' + esc(fromTo) + '</td></tr>';
    }).join('');
  } catch (e) { /* ignore */ }
}

function fmtBytes(n) {
  if (!n) return '0';
  const units = ['B','KB','MB','GB','TB','PB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 ? 1 : 0) + ' ' + units[i];
}

let incompleteSyncPoll = null;
let incompleteSyncObserved = false;

function _syncBadge(status) {
  const map = {
    pending: '<span class="badge pending">Pending</span>',
    running: '<span class="badge scanning">Running...</span>',
    ok:      '<span class="badge success">Synced</span>',
    failed:  '<span class="badge failed">Failed</span>',
  };
  return map[status] || '<span class="badge">-</span>';
}

async function loadIncompleteDevices() {
  try {
    const r = await fetch('/api/discover/incomplete-devices');
    if (!r.ok) return;
    const d = await r.json();
    const card = document.getElementById('incomplete-card');
    const devices = d.devices || [];
    if (!devices.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    const esc = (s) => { const x = document.createElement('div'); x.textContent = s == null ? '' : String(s); return x.innerHTML; };

    document.getElementById('incomplete-summary').textContent =
      devices.length + ' device' + (devices.length === 1 ? '' : 's') +
      ' with incomplete onboarding — no primary IP and no interface IPs assigned.';

    const nautobotBase = `${window.location.protocol}//${window.location.hostname}:8443`;
    document.getElementById('incomplete-table').innerHTML = devices.map(dev => {
      const deviceUrl = `${nautobotBase}/dcim/devices/${dev.id}/`;
      return '<tr data-device-id="' + esc(dev.id) + '" data-device-name="' + esc(dev.name) + '">' +
        '<td><a href="' + deviceUrl + '" target="_blank" rel="noopener">' + esc(dev.name) + '</a></td>' +
        '<td>' + esc(dev.device_type) + '</td>' +
        '<td>' + esc(dev.platform) + '</td>' +
        '<td>' + esc(dev.location) + '</td>' +
        '<td>' + esc(dev.status) + '</td>' +
        '<td class="sync-status">' + _syncBadge('pending') + '</td>' +
        '<td><button class="btn exclude-device-btn" data-name="' + esc(dev.name) + '">Exclude</button></td>' +
        '</tr>';
    }).join('');
  } catch (e) { console.error('Incomplete devices load failed:', e); }
}

async function pollSyncIncomplete() {
  try {
    const r = await fetch('/api/discover/sync-incomplete');
    if (!r.ok) return;
    const s = await r.json();

    const bar = document.getElementById('sync-incomplete-bar');
    const phaseEl = document.getElementById('sync-incomplete-phase');
    const countEl = document.getElementById('sync-incomplete-count');

    if (s.running) incompleteSyncObserved = true;

    const total = s.total || 0;
    const done = s.completed || 0;
    const pct = total > 0 ? Math.round((done / total) * 100) : 0;
    bar.style.width = pct + '%';
    countEl.textContent = done + ' / ' + total + ' (' + s.succeeded + ' ok, ' + s.failed + ' failed)';

    // Per-row badges
    (s.devices || []).forEach(dev => {
      const row = document.querySelector('tr[data-device-id="' + dev.id + '"] .sync-status');
      if (row) row.innerHTML = _syncBadge(dev.status);
    });

    if (s.running) {
      phaseEl.textContent = 'Syncing ' + (s.current_device || '...');
      return;
    }

    if (!incompleteSyncObserved) {
      // POST returned before background task flipped running=true; keep polling.
      return;
    }

    // Done — stop polling, refresh the advisory card. If everything succeeded
    // and Nautobot has caught up, the card will disappear on the next load.
    phaseEl.textContent = 'Complete (' + s.succeeded + ' ok, ' + s.failed + ' failed)';
    if (incompleteSyncPoll) { clearInterval(incompleteSyncPoll); incompleteSyncPoll = null; }
    incompleteSyncObserved = false;
    document.getElementById('sync-incomplete-btn').disabled = false;
    document.getElementById('sync-incomplete-btn').textContent = 'Sync All';
    // Nautobot's job runs async — give it a couple seconds before re-checking
    setTimeout(() => {
      loadIncompleteDevices();
      loadDevices();
    }, 4000);
  } catch (e) { /* ignore */ }
}

document.addEventListener('click', async (ev) => {
  if (ev.target && ev.target.classList && ev.target.classList.contains('exclude-device-btn')) {
    const name = ev.target.getAttribute('data-name');
    if (!confirm('Exclude device "' + name + '" from MNM advisories?\n\nThe Incomplete Devices card will hide this device. To also stop the sweep from re-onboarding it, add its IP separately on the Discovery page.')) return;
    const reason = prompt('Reason for excluding this device? (optional)') || '';
    try {
      const r = await fetch('/api/discover/excludes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier: name, type: 'device_name', reason }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        alert('Failed to exclude: ' + (err.detail || r.status));
        return;
      }
      loadIncompleteDevices();
    } catch (e) {
      alert('Failed: ' + e);
    }
    return;
  }
  if (ev.target && ev.target.id === 'sync-incomplete-btn') {
    const btn = ev.target;
    btn.disabled = true;
    btn.textContent = 'Syncing...';
    incompleteSyncObserved = false;
    document.getElementById('sync-incomplete-progress').style.display = 'block';
    try {
      const r = await fetch('/api/discover/sync-incomplete', { method: 'POST' });
      if (r.status === 401) { window.location.href = '/login'; return; }
      if (!incompleteSyncPoll) {
        incompleteSyncPoll = setInterval(pollSyncIncomplete, 1500);
      }
    } catch (e) {
      console.error('Sync incomplete failed:', e);
      btn.disabled = false;
      btn.textContent = 'Sync All';
    }
  }
});

async function loadSubnets() {
  try {
    const r = await fetch('/api/discover/subnets');
    if (!r.ok) return;
    const d = await r.json();
    const subnets = d.subnets || [];
    const card = document.getElementById('subnets-card');
    if (!subnets.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    const esc = (s) => { const x = document.createElement('div'); x.textContent = s == null ? '' : String(s); return x.innerHTML; };
    document.getElementById('subnets-table').innerHTML = subnets.map(s =>
      '<tr><td><code>' + esc(s.cidr) + '</code></td>' +
      '<td>' + s.ip_count + '</td>' +
      '<td style="font-size:0.8rem;color:var(--text-muted)">' + s.sample_ips.map(esc).join(', ') + '</td>' +
      '<td><a href="/discover?cidr=' + encodeURIComponent(s.cidr) + '" class="btn">Add to sweep</a></td></tr>'
    ).join('');
  } catch (e) { console.error('Subnet advisory load failed:', e); }
}

async function loadAutoDiscovered() {
  try {
    var r = await fetch('/api/discover/auto/recent?hours=24');
    if (!r.ok) return;
    var d = await r.json();
    var nodes = d.nodes || [];
    var card = document.getElementById('auto-discover-card');
    if (!nodes.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    document.getElementById('auto-discover-count').textContent = nodes.length + ' node(s) in last 24h';
    var esc = function(s) { var x = document.createElement('div'); x.textContent = s == null ? '' : String(s); return x.innerHTML; };
    document.getElementById('auto-discover-table').innerHTML = nodes.map(function(n) {
      var statusBadge = n.status === 'succeeded'
        ? '<span class="badge success">onboarded</span>'
        : '<span class="badge failed">failed</span>';
      return '<tr>'
        + '<td><strong>' + esc(n.name) + '</strong></td>'
        + '<td><code>' + esc(n.ip) + '</code></td>'
        + '<td>' + esc(n.parent_node) + '</td>'
        + '<td>' + esc(n.hop_depth) + '</td>'
        + '<td>' + statusBadge + '</td>'
        + '</tr>';
    }).join('');
  } catch (e) { console.error('Auto-discover load failed:', e); }
}

async function loadRouteAdvisories() {
  try {
    var r = await fetch('/api/routes/advisories');
    if (!r.ok) return;
    var d = await r.json();
    var advisories = d.advisories || [];
    var card = document.getElementById('route-advisories-card');
    if (!advisories.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    document.getElementById('route-advisory-count').textContent = advisories.length + ' unknown next-hop(s)';
    var esc = function(s) { var x = document.createElement('div'); x.textContent = s == null ? '' : String(s); return x.innerHTML; };
    // Infer a /24 from the next-hop for the sweep suggestion
    function inferCidr(ip) {
      var parts = ip.split('.');
      if (parts.length === 4) return parts[0] + '.' + parts[1] + '.' + parts[2] + '.0/24';
      return ip + '/32';
    }
    document.getElementById('route-advisories-table').innerHTML = advisories.slice(0, 50).map(function(a) {
      return '<tr>'
        + '<td>' + esc(a.node_name) + '</td>'
        + '<td><code>' + esc(a.prefix) + '</code></td>'
        + '<td><code>' + esc(a.next_hop) + '</code></td>'
        + '<td>' + esc(a.protocol) + '</td>'
        + '<td>' + esc(a.vrf) + '</td>'
        + '<td><a href="/discover?cidr=' + encodeURIComponent(inferCidr(a.next_hop)) + '" class="btn small">Add to sweep</a></td>'
        + '</tr>';
    }).join('');
  } catch (e) { console.error('Route advisory load failed:', e); }
}

async function loadMaintenance() {
  try {
    const r = await fetch('/api/admin/maintenance');
    if (!r.ok) return;
    const d = await r.json();
    if (!d.db_ready) return;
    const stats = d.stats || {};
    document.getElementById('maint-endpoint-rows').textContent = stats.endpoint_rows ?? '-';
    document.getElementById('maint-event-rows').textContent = stats.event_rows ?? '-';
    document.getElementById('maint-observation-rows').textContent = stats.observation_rows ?? '-';
    document.getElementById('maint-watch-rows').textContent = stats.watch_rows ?? '-';
    document.getElementById('maint-route-rows').textContent = stats.route_rows ?? '-';
    document.getElementById('maint-bgp-rows').textContent = stats.bgp_neighbor_rows ?? '-';
    document.getElementById('maintenance-retention').textContent =
      'retention: ' + d.retention_days + ' days · prune every ' + d.prune_interval_hours + 'h';
    const lastRunEl = document.getElementById('maint-last-run');
    if (d.last_run) {
      const sum = d.last_summary || {};
      lastRunEl.textContent = 'Last prune ' + timeAgo(d.last_run) + ' — ' +
        (sum.events || 0) + ' events, ' + (sum.observations || 0) + ' obs, ' +
        (sum.watches || 0) + ' watches, ' + (sum.sentinels || 0) + ' sentinels';
    } else {
      lastRunEl.textContent = 'Last prune: never';
    }
    const meta = [];
    if (stats.oldest_event) meta.push('Oldest event: ' + new Date(stats.oldest_event).toLocaleString());
    if (stats.oldest_observation) meta.push('Oldest observation: ' + new Date(stats.oldest_observation).toLocaleString());
    document.getElementById('maint-meta').textContent = meta.join(' · ');
  } catch (e) { console.error('Maintenance load failed:', e); }
}

document.addEventListener('click', async (ev) => {
  const t = ev.target;
  if (t.id === 'maint-preview-btn') {
    t.disabled = true; t.textContent = 'Previewing...';
    try {
      const r = await fetch('/api/admin/prune/preview');
      const d = await r.json();
      const wp = d.would_prune || {};
      const el = document.getElementById('maint-preview-result');
      el.className = 'alert';
      el.style.display = 'block';
      el.textContent = 'Would delete: ' + (wp.events || 0) + ' events, ' +
        (wp.observations || 0) + ' observations, ' + (wp.watches || 0) +
        ' orphaned watches, ' + (wp.sentinels || 0) + ' stale sentinels' +
        ' (retention ' + d.retention_days + ' days)';
    } catch (e) {
      console.error('Preview failed:', e);
    } finally {
      t.disabled = false; t.textContent = 'Preview Prune';
    }
  }
  if (t.id === 'maint-prune-btn') {
    if (!confirm('Run database prune now? This deletes events, IP observations, orphaned watches, and stale sentinel rows older than the retention threshold. Cannot be undone.')) return;
    t.disabled = true; t.textContent = 'Pruning...';
    try {
      const r = await fetch('/api/admin/prune', { method: 'POST' });
      if (!r.ok) {
        const err = await r.json();
        alert('Prune failed: ' + (err.detail || r.status));
        return;
      }
      const d = await r.json();
      const p = d.pruned || {};
      const el = document.getElementById('maint-preview-result');
      el.className = 'alert success';
      el.style.display = 'block';
      el.textContent = 'Pruned: ' + (p.events || 0) + ' events, ' +
        (p.observations || 0) + ' observations, ' + (p.watches || 0) +
        ' watches, ' + (p.sentinels || 0) + ' sentinels';
      loadMaintenance();
    } catch (e) {
      alert('Prune failed: ' + e);
    } finally {
      t.disabled = false; t.textContent = 'Run Prune Now';
    }
  }
});

async function loadProxmox() {
  try {
    const r = await fetch('/api/proxmox/status');
    if (!r.ok) return;
    const d = await r.json();
    const card = document.getElementById('proxmox-card');
    if (!d.configured) { card.style.display = 'none'; return; }
    card.style.display = '';
    document.getElementById('proxmox-nodes').textContent = d.node_count;
    document.getElementById('proxmox-vms').textContent = d.vm_count;
    document.getElementById('proxmox-cts').textContent = d.container_count;
    document.getElementById('proxmox-last').textContent = d.last_run ? timeAgo(d.last_run) : '-';
    const grafanaUrl = `${window.location.protocol}//${window.location.hostname}:8080/grafana/d/mnm-proxmox-overview`;
    document.getElementById('proxmox-link').innerHTML = '<a href="' + grafanaUrl + '" target="_blank" rel="noopener">Grafana &rarr;</a>';

    const esc = (s) => { const x = document.createElement('div'); x.textContent = s == null ? '' : String(s); return x.innerHTML; };

    // Per-node table
    if (d.nodes && d.nodes.length) {
      let html = '<table><thead><tr><th>Node</th><th>CPU</th><th>Memory</th><th>Uptime</th></tr></thead><tbody>';
      d.nodes.forEach(n => {
        const cpuPct = ((n.cpu || 0) * 100).toFixed(0) + '%';
        const memPct = n.memory_total ? ((n.memory_used / n.memory_total) * 100).toFixed(0) + '%' : '-';
        const days = Math.floor((n.uptime || 0) / 86400);
        html += '<tr><td>' + esc(n.name) + '</td><td>' + cpuPct + '</td><td>' +
                fmtBytes(n.memory_used) + ' / ' + fmtBytes(n.memory_total) + ' (' + memPct + ')</td><td>' +
                days + 'd</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('proxmox-nodes-table').innerHTML = html;
    }

    // Storage line
    if (d.zfs_pools && d.zfs_pools.length) {
      let used = 0, total = 0;
      d.zfs_pools.forEach(p => { used += (p.alloc || 0); total += (p.size || 0); });
      const pct = total ? ((used / total) * 100).toFixed(0) + '%' : '-';
      document.getElementById('proxmox-storage').textContent =
        'Storage: ' + d.zfs_pools.length + ' ZFS pool' + (d.zfs_pools.length === 1 ? '' : 's') +
        ', ' + fmtBytes(used) + ' / ' + fmtBytes(total) + ' used (' + pct + ')';
    } else {
      document.getElementById('proxmox-storage').textContent = '';
    }

    // Alerts: API permission errors, then unhealthy pools or disks
    const alerts = [];
    if (d.warnings && d.warnings.length) {
      const permIssues = d.warnings.filter(w => /Permission check failed|HTTP 403/.test(w));
      if (permIssues.length) {
        alerts.push('<strong>Proxmox API token is missing permissions.</strong> ' +
          'Grant the PVEAuditor role at path <code>/</code> to ' +
          '<code>' + esc(d.warnings[0].split(':')[0]) + '</code>\'s token. ' +
          'See <a href="https://github.com/commitconfirm/mnm/blob/main/docs/CONNECTORS.md#setting-up-the-api-token-in-proxmox">CONNECTORS.md</a>.');
      } else if (d.warnings.length) {
        alerts.push('Proxmox API warnings: ' + esc(d.warnings.slice(0, 3).join('; ')));
      }
    }
    (d.zfs_pools || []).forEach(p => {
      const h = (p.health || '').toUpperCase();
      if (h && h !== 'ONLINE') alerts.push('ZFS pool <code>' + esc(p.name) + '</code> on ' + esc(p.node) + ': ' + esc(p.health));
    });
    (d.disks || []).forEach(dk => {
      const h = (dk.health || '').toUpperCase();
      if (h && h !== 'PASSED' && h !== 'OK' && h !== 'UNKNOWN' && h !== '') {
        alerts.push('Disk <code>' + esc(dk.devpath) + '</code> on ' + esc(dk.node) + ': ' + esc(dk.health));
      }
    });
    const alertEl = document.getElementById('proxmox-alerts');
    if (alerts.length) {
      alertEl.innerHTML = '<div class="alert warning">' + alerts.join('<br>') + '</div>';
    } else {
      alertEl.innerHTML = '';
    }
  } catch (e) { console.error('Proxmox load failed:', e); }
}

// ---------------------------------------------------------------------------
// Polling Status
// ---------------------------------------------------------------------------

function pollStatusDot(job, intervalSec) {
  if (!job) return '<span style="color:var(--text-muted)">-</span>';
  if (!job.enabled) return '<span title="disabled" style="color:var(--text-muted)">&#9679; off</span>';
  if (job.last_error) return '<span title="' + escHtml(job.last_error) + '" style="color:var(--red, #b71c1c)">&#9679; err</span>';
  if (!job.last_success) return '<span style="color:var(--text-muted)">&#9679; -</span>';
  var ago = Date.now() - new Date(job.last_success).getTime();
  var threshold = (job.interval_sec || intervalSec || 300) * 2 * 1000;
  var color = ago < threshold ? 'var(--green, #1c8a3a)' : '#e6a800';
  var label = timeAgo(job.last_success);
  return '<span title="' + (job.last_duration ? job.last_duration.toFixed(1) + 's' : '') + '" style="color:' + color + '">&#9679; ' + label + '</span>';
}

async function loadPolling() {
  try {
    var resp = await fetch('/api/polling/status');
    if (!resp.ok) return;
    var data = await resp.json();
    var devices = data.devices || [];
    var card = document.getElementById('polling-card');
    var tbody = document.getElementById('polling-table');
    if (!devices.length) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    tbody.innerHTML = devices.map(function(d) {
      var j = d.jobs || {};
      return '<tr>'
        + '<td><strong>' + escHtml(d.device_name) + '</strong></td>'
        + '<td>' + pollStatusDot(j.arp) + '</td>'
        + '<td>' + pollStatusDot(j.mac) + '</td>'
        + '<td>' + pollStatusDot(j.dhcp) + '</td>'
        + '<td>' + pollStatusDot(j.lldp) + '</td>'
        + '<td>' + pollStatusDot(j.routes) + '</td>'
        + '<td>' + pollStatusDot(j.bgp) + '</td>'
        + '<td><button class="btn small" onclick="triggerPoll(\'' + escHtml(d.device_name) + '\')">Poll Now</button></td>'
        + '</tr>';
    }).join('');
  } catch (e) { console.error('Failed to load polling:', e); }
}

async function triggerPoll(deviceName) {
  try {
    await fetch('/api/polling/trigger/' + encodeURIComponent(deviceName), { method: 'POST' });
    setTimeout(loadPolling, 1000);
  } catch (e) { alert('Failed: ' + e.message); }
}

checkAuth().then(() => {
  loadContainers();
  loadDevices();
  loadIPCount();
  loadNeighbors();
  loadSweepInfo();
  loadEndpointSummary();
  loadRecentEvents();
  loadPolling();
  loadIncompleteDevices();
  loadSubnets();
  loadRouteAdvisories();
  loadAutoDiscovered();
  loadProxmox();
  loadMaintenance();
  startAutoRefresh();
});
