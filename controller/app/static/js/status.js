/* MNM Dashboard — NOC-style status overview (Phase 2.9) */

async function checkAuth() {
  const resp = await fetch('/api/auth/check');
  const data = await resp.json();
  if (!data.authenticated) window.location.href = '/login';
}

function escHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function timeAgo(isoStr) {
  if (!isoStr) return '-';
  const diff = Date.now() - new Date(isoStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  return Math.floor(hours / 24) + 'd ago';
}

function fmtUptime(secs) {
  if (!secs) return '-';
  var d = Math.floor(secs / 86400), h = Math.floor((secs % 86400) / 3600), m = Math.floor((secs % 3600) / 60);
  if (d > 0) return d + 'd ' + h + 'h';
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm';
}

function fmtBytes(n) {
  if (!n) return '0';
  const units = ['B','KB','MB','GB','TB','PB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 ? 1 : 0) + ' ' + units[i];
}

// ---------------------------------------------------------------------------
// Container Health Bar
// ---------------------------------------------------------------------------

var SVC_ICONS = {
  'mnm-nautobot': 'nautobot', 'mnm-grafana': 'grafana', 'mnm-prometheus': 'prometheus',
};
var SVC_LINKS = {}; // populated from /api/service-urls

function initServiceLinks() {
  var urls = MNMServiceURLs.get();
  SVC_LINKS = {
    'mnm-nautobot': urls.nautobot,
    'mnm-grafana': urls.grafana,
    'mnm-prometheus': urls.prometheus,
  };
}

function containerDot(health, status) {
  if (health === 'healthy') return '<span class="dot green"></span>';
  if (health === 'unhealthy') return '<span class="dot red"></span>';
  if (status === 'running') return '<span class="dot yellow"></span>';
  if (status === 'exited') return '<span class="dot red"></span>';
  return '<span class="dot grey"></span>';
}

// User-facing services (have their own web UI / external interface)
var USER_FACING = ['mnm-controller', 'mnm-nautobot', 'mnm-grafana', 'mnm-prometheus', 'mnm-traefik'];

function renderContainerItem(c) {
  var name = c.name || '';
  var shortName = name.replace('mnm-', '');
  var iconKey = SVC_ICONS[name];
  var iconHtml = iconKey ? '<img src="/static/icons/services/' + iconKey + '.svg" alt="">' : '';
  var linkUrl = SVC_LINKS[name];
  var nameHtml = linkUrl
    ? '<a href="' + linkUrl + '" target="_blank" rel="noopener" class="svc-name">' + escHtml(shortName) + '</a>'
    : '<span class="svc-name">' + escHtml(shortName) + '</span>';
  var tip = escHtml(shortName) + ': ' + escHtml(c.status);
  if (c.health && c.health !== 'N/A') tip += ' / ' + escHtml(c.health);
  if (c.image) tip += '\n' + escHtml(c.image);
  if (c.ports && c.ports.length) tip += '\nPorts: ' + escHtml(c.ports.join(', '));
  return '<div class="svc-item" title="' + tip.replace(/"/g, '&quot;') + '">'
    + containerDot(c.health, c.status) + iconHtml + nameHtml + '</div>';
}

async function loadContainers() {
  try {
    var resp = await fetch('/api/status');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    var data = await resp.json();
    var containers = data.containers || [];
    var total = containers.length;
    var healthy = containers.filter(function(c) { return c.health === 'healthy'; }).length;
    var running = containers.filter(function(c) { return c.status === 'running'; }).length;

    var services = containers.filter(function(c) { return USER_FACING.indexOf(c.name) !== -1; });
    var infra = containers.filter(function(c) { return USER_FACING.indexOf(c.name) === -1; });

    var svcHealthy = services.filter(function(c) { return c.health === 'healthy' || c.status === 'running'; }).length;
    document.getElementById('svc-summary').textContent = svcHealthy + '/' + services.length + ' up';

    document.getElementById('svc-bar-services').innerHTML = services.map(renderContainerItem).join('');
    document.getElementById('svc-bar-infra').innerHTML = infra.map(renderContainerItem).join('');

    var infraHealthy = infra.filter(function(c) { return c.health === 'healthy'; }).length;
    var infraRunning = infra.filter(function(c) { return c.status === 'running'; }).length;
    document.getElementById('infra-summary').textContent = infraHealthy + '/' + infra.length + ' healthy, ' + infraRunning + '/' + infra.length + ' running';
  } catch (e) {
    console.error('Failed to load containers:', e);
  }
}

// ---------------------------------------------------------------------------
// Node Polling Health
// ---------------------------------------------------------------------------

function compositeHealth(jobs) {
  if (!jobs) return 'gray';
  var types = ['arp','mac','dhcp','lldp','routes','bgp'];
  var hasAny = false, allFail = true, anyStale = false;
  types.forEach(function(t) {
    var j = jobs[t];
    if (!j || !j.enabled) return;
    hasAny = true;
    if (j.last_error) { /* failing */ }
    else if (j.last_success) {
      allFail = false;
      var ago = Date.now() - new Date(j.last_success).getTime();
      if (ago > (j.interval_sec || 300) * 2 * 1000) anyStale = true;
    } else { allFail = false; }
  });
  if (!hasAny) return 'gray';
  if (allFail) return 'red';
  if (anyStale) return 'yellow';
  return 'green';
}

function latestPoll(jobs) {
  if (!jobs) return null;
  var latest = null;
  Object.keys(jobs).forEach(function(k) {
    var ts = jobs[k] && jobs[k].last_success;
    if (ts && (!latest || ts > latest)) latest = ts;
  });
  return latest;
}

async function loadPolling() {
  try {
    var resp = await fetch('/api/nodes');
    if (!resp.ok) return;
    var data = await resp.json();
    var nodes = data.nodes || [];
    var card = document.getElementById('polling-card');
    var tbody = document.getElementById('polling-table');
    if (!nodes.length) { card.style.display = 'none'; return; }
    card.style.display = '';

    var failing = 0;
    nodes.forEach(function(n) {
      var h = compositeHealth(n.jobs);
      if (h === 'red' || h === 'yellow') failing++;
    });

    var sumEl = document.getElementById('polling-summary');
    if (failing === 0) {
      sumEl.className = 'health-summary all-good';
      sumEl.textContent = nodes.length + ' node' + (nodes.length === 1 ? '' : 's') + ' \u2014 all healthy';
    } else {
      sumEl.className = 'health-summary has-issues';
      sumEl.textContent = nodes.length + ' node' + (nodes.length === 1 ? '' : 's') + ' \u2014 ' + failing + ' with issues';
    }

    tbody.innerHTML = nodes.map(function(n) {
      var h = compositeHealth(n.jobs);
      var lp = latestPoll(n.jobs);
      var platform = n.platform || '';
      var ip = (n.primary_ip || '').replace(/\/\d+$/, '');
      return '<tr>'
        + '<td>' + MNMIcons.deviceIcon(n.role === 'Network Device' ? 'switch' : 'router', 'sm') + '</td>'
        + '<td><a href="/nodes/' + encodeURIComponent(n.name) + '">' + escHtml(n.name) + '</a></td>'
        + '<td><code>' + escHtml(ip) + '</code></td>'
        + '<td>' + escHtml(platform) + '</td>'
        + '<td class="center"><span class="dot ' + h + '"></span></td>'
        + '<td>' + timeAgo(lp) + '</td>'
        + '<td><button class="btn small" onclick="triggerPoll(\'' + escHtml(n.name) + '\')">Poll Now</button></td>'
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

// ---------------------------------------------------------------------------
// System Health
// ---------------------------------------------------------------------------

async function loadSystemHealth() {
  try {
    var [healthResp, maintResp, jobsResp] = await Promise.all([
      fetch('/api/health'),
      fetch('/api/admin/maintenance'),
      fetch('/api/jobs'),
    ]);
    if (healthResp.ok) {
      var h = await healthResp.json();
      document.getElementById('sys-uptime').textContent = fmtUptime(h.uptime_seconds);
      document.getElementById('sys-endpoints').textContent = h.endpoints_tracked || 0;
      document.getElementById('sys-sweep').textContent = h.last_sweep ? timeAgo(h.last_sweep) : 'never';
    }
    if (maintResp.ok) {
      var m = await maintResp.json();
      var stats = m.stats || {};
      document.getElementById('sys-routes').textContent = stats.route_rows || 0;
      document.getElementById('sys-arp-rows').textContent = stats.arp_rows || 0;
      document.getElementById('sys-prune').textContent = m.last_run ? timeAgo(m.last_run) : 'never';
    }
  } catch (e) { console.error('System health load failed:', e); }
}

// ---------------------------------------------------------------------------
// Interface Errors
// ---------------------------------------------------------------------------

async function loadInterfaceErrors() {
  var content = document.getElementById('iface-errors-content');
  try {
    var resp = await fetch('/api/dashboard/interface-errors');
    if (!resp.ok) { content.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">Could not query Prometheus</span>'; return; }
    var data = await resp.json();
    if (data.error) {
      content.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">Prometheus unreachable: ' + escHtml(data.error) + '</span>';
      return;
    }
    var ifaces = data.interfaces || [];
    if (!ifaces.length) {
      content.innerHTML = '<div class="iface-ok">No interface errors detected across all nodes</div>';
      return;
    }
    var html = '<table><thead><tr><th>Node</th><th>Interface</th><th>Errors In</th><th>Errors Out</th><th>Discards In</th><th>Discards Out</th></tr></thead><tbody>';
    ifaces.forEach(function(i) {
      html += '<tr><td>' + escHtml(i.node_name) + '</td><td>' + escHtml(i.interface) + '</td>'
        + '<td>' + (i.errors_in || 0) + '</td><td>' + (i.errors_out || 0) + '</td>'
        + '<td>' + (i.discards_in || 0) + '</td><td>' + (i.discards_out || 0) + '</td></tr>';
    });
    html += '</tbody></table>';
    content.innerHTML = html;
  } catch (e) {
    content.innerHTML = '<span style="color:var(--text-muted);font-size:0.85rem">Could not load interface errors</span>';
  }
}

// ---------------------------------------------------------------------------
// Recent Events
// ---------------------------------------------------------------------------

async function loadRecentEvents() {
  try {
    var r = await fetch('/api/endpoints/events?since=24h&limit=10');
    if (!r.ok) return;
    var data = await r.json();
    var tbody = document.getElementById('recent-events-tbody');
    if (!data.events || !data.events.length) {
      tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">No events in the last 24 hours</td></tr>';
      return;
    }
    tbody.innerHTML = data.events.slice(0, 10).map(function(e) {
      var when = new Date(e.timestamp).toLocaleString();
      var watched = e.details && e.details.watched === true;
      var star = watched ? ' <span title="Watched">\ud83d\udc41</span>' : '';
      var rowStyle = watched ? ' style="background:rgba(255,200,0,0.08)"' : '';
      var macLink = '<a href="/endpoints/' + encodeURIComponent(e.mac_address) + '"><code>' + escHtml(e.mac_address) + '</code></a>' + star;
      var fromTo = (e.old_value || '-') + ' \u2192 ' + (e.new_value || '-');
      return '<tr' + rowStyle + '><td>' + escHtml(when) + '</td><td>' + macLink + '</td><td><span class="badge">' + escHtml(e.event_type) + '</span></td><td>' + escHtml(fromTo) + '</td></tr>';
    }).join('');
  } catch (e) { /* ignore */ }
}

// ---------------------------------------------------------------------------
// Proxmox (keep existing logic)
// ---------------------------------------------------------------------------

async function loadProxmox() {
  try {
    var r = await fetch('/api/proxmox/status');
    if (!r.ok) return;
    var d = await r.json();
    var card = document.getElementById('proxmox-card');
    if (!d.configured) { card.style.display = 'none'; return; }
    card.style.display = '';
    document.getElementById('proxmox-nodes').textContent = d.node_count;
    document.getElementById('proxmox-vms').textContent = d.vm_count;
    document.getElementById('proxmox-cts').textContent = d.container_count;
    document.getElementById('proxmox-last').textContent = d.last_run ? timeAgo(d.last_run) : '-';
    var grafanaUrl = MNMServiceURLs.grafanaDashboard('mnm-proxmox-overview');
    document.getElementById('proxmox-link').innerHTML = '<a href="' + grafanaUrl + '" target="_blank" rel="noopener">Grafana &rarr;</a>';

    if (d.nodes && d.nodes.length) {
      var html = '<table><thead><tr><th>Node</th><th>CPU</th><th>Memory</th><th>Uptime</th></tr></thead><tbody>';
      d.nodes.forEach(function(n) {
        var cpuPct = ((n.cpu || 0) * 100).toFixed(0) + '%';
        var memPct = n.memory_total ? ((n.memory_used / n.memory_total) * 100).toFixed(0) + '%' : '-';
        var days = Math.floor((n.uptime || 0) / 86400);
        html += '<tr><td>' + escHtml(n.name) + '</td><td>' + cpuPct + '</td><td>'
          + fmtBytes(n.memory_used) + ' / ' + fmtBytes(n.memory_total) + ' (' + memPct + ')</td><td>'
          + days + 'd</td></tr>';
      });
      html += '</tbody></table>';
      document.getElementById('proxmox-nodes-table').innerHTML = html;
    }

    if (d.zfs_pools && d.zfs_pools.length) {
      var used = 0, total = 0;
      d.zfs_pools.forEach(function(p) { used += (p.alloc || 0); total += (p.size || 0); });
      var pct = total ? ((used / total) * 100).toFixed(0) + '%' : '-';
      document.getElementById('proxmox-storage').textContent =
        'Storage: ' + d.zfs_pools.length + ' ZFS pool' + (d.zfs_pools.length === 1 ? '' : 's')
        + ', ' + fmtBytes(used) + ' / ' + fmtBytes(total) + ' used (' + pct + ')';
    }

    var alerts = [];
    if (d.warnings && d.warnings.length) {
      var permIssues = d.warnings.filter(function(w) { return /Permission check failed|HTTP 403/.test(w); });
      if (permIssues.length) {
        alerts.push('<strong>Proxmox API token is missing permissions.</strong> Grant the PVEAuditor role.');
      } else {
        alerts.push('Proxmox API warnings: ' + escHtml(d.warnings.slice(0, 3).join('; ')));
      }
    }
    (d.zfs_pools || []).forEach(function(p) {
      var h = (p.health || '').toUpperCase();
      if (h && h !== 'ONLINE') alerts.push('ZFS pool <code>' + escHtml(p.name) + '</code>: ' + escHtml(p.health));
    });
    var alertEl = document.getElementById('proxmox-alerts');
    alertEl.innerHTML = alerts.length ? '<div class="alert warning">' + alerts.join('<br>') + '</div>' : '';
  } catch (e) { console.error('Proxmox load failed:', e); }
}

// ---------------------------------------------------------------------------
// Advisories (keep existing logic)
// ---------------------------------------------------------------------------

async function loadNeighbors() {
  try {
    var resp = await fetch('/api/discover/neighbors');
    if (!resp.ok) return;
    var data = await resp.json();
    var neighbors = data.neighbors || [];
    var card = document.getElementById('neighbors-card');
    if (!neighbors.length) { card.style.display = 'none'; return; }
    card.style.display = 'block';
    var badge = document.getElementById('neighbor-count-badge');
    if (badge) badge.textContent = neighbors.length;
    document.getElementById('neighbor-table').innerHTML = neighbors.map(function(n) {
      return '<tr><td>' + escHtml(n.neighbor_name) + '</td><td>' + escHtml(n.connected_to)
        + '</td><td>' + (n.classification ? '<span class="badge ' + n.classification + '">' + n.classification.replace(/_/g, ' ') + '</span>' : '-')
        + '</td><td>' + escHtml(n.mac_vendor || '-') + '</td></tr>';
    }).join('');
  } catch (e) { console.error('Failed to load neighbors:', e); }
}

function _syncBadge(status) {
  var map = {
    pending: '<span class="badge pending">Pending</span>',
    running: '<span class="badge scanning">Running...</span>',
    ok:      '<span class="badge success">Synced</span>',
    failed:  '<span class="badge failed">Failed</span>',
  };
  return map[status] || '<span class="badge">-</span>';
}

async function loadIncompleteDevices() {
  try {
    var r = await fetch('/api/discover/incomplete-devices');
    if (!r.ok) return;
    var d = await r.json();
    var card = document.getElementById('incomplete-card');
    var devices = d.devices || [];
    if (!devices.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    var badge = document.getElementById('incomplete-count-badge');
    if (badge) badge.textContent = devices.length;
    document.getElementById('incomplete-summary').textContent =
      devices.length + ' device' + (devices.length === 1 ? '' : 's') + ' with incomplete onboarding.';
    var nautobotBase = MNMServiceURLs.nautobot();
    document.getElementById('incomplete-table').innerHTML = devices.map(function(dev) {
      var deviceUrl = nautobotBase + '/dcim/devices/' + dev.id + '/';
      return '<tr data-device-id="' + escHtml(dev.id) + '" data-device-name="' + escHtml(dev.name) + '">'
        + '<td><a href="' + deviceUrl + '" target="_blank" rel="noopener">' + escHtml(dev.name) + '</a></td>'
        + '<td>' + escHtml(dev.device_type) + '</td><td>' + escHtml(dev.platform) + '</td>'
        + '<td>' + escHtml(dev.location) + '</td><td>' + escHtml(dev.status) + '</td>'
        + '<td class="sync-status">' + _syncBadge('pending') + '</td>'
        + '<td><button class="btn small exclude-device-btn" data-name="' + escHtml(dev.name) + '">Exclude</button></td></tr>';
    }).join('');
  } catch (e) { console.error('Incomplete devices load failed:', e); }
}

var incompleteSyncPoll = null;
var incompleteSyncObserved = false;

async function pollSyncIncomplete() {
  try {
    var r = await fetch('/api/discover/sync-incomplete');
    if (!r.ok) return;
    var s = await r.json();
    if (s.running) incompleteSyncObserved = true;
    var total = s.total || 0, done = s.completed || 0;
    var pct = total > 0 ? Math.round((done / total) * 100) : 0;
    document.getElementById('sync-incomplete-bar').style.width = pct + '%';
    document.getElementById('sync-incomplete-count').textContent = done + ' / ' + total;
    (s.devices || []).forEach(function(dev) {
      var row = document.querySelector('tr[data-device-id="' + dev.id + '"] .sync-status');
      if (row) row.innerHTML = _syncBadge(dev.status);
    });
    if (s.running) { document.getElementById('sync-incomplete-phase').textContent = 'Syncing ' + (s.current_device || '...'); return; }
    if (!incompleteSyncObserved) return;
    document.getElementById('sync-incomplete-phase').textContent = 'Complete';
    if (incompleteSyncPoll) { clearInterval(incompleteSyncPoll); incompleteSyncPoll = null; }
    incompleteSyncObserved = false;
    document.getElementById('sync-incomplete-btn').disabled = false;
    document.getElementById('sync-incomplete-btn').textContent = 'Sync All';
    setTimeout(function() { loadIncompleteDevices(); loadPolling(); }, 4000);
  } catch (e) { /* ignore */ }
}

async function loadSubnets() {
  try {
    var r = await fetch('/api/discover/subnets');
    if (!r.ok) return;
    var d = await r.json();
    var subnets = d.subnets || [];
    var card = document.getElementById('subnets-card');
    if (!subnets.length) { card.style.display = 'none'; return; }
    card.style.display = '';
    var badge = document.getElementById('subnet-count-badge');
    if (badge) badge.textContent = subnets.length;
    document.getElementById('subnets-table').innerHTML = subnets.map(function(s) {
      return '<tr><td><code>' + escHtml(s.cidr) + '</code></td><td>' + s.ip_count + '</td>'
        + '<td style="font-size:0.8rem;color:var(--text-muted)">' + s.sample_ips.map(escHtml).join(', ') + '</td>'
        + '<td><a href="/discover?cidr=' + encodeURIComponent(s.cidr) + '" class="btn small">Add to sweep</a></td></tr>';
    }).join('');
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
    document.getElementById('auto-discover-count').textContent = nodes.length + ' in last 24h';
    document.getElementById('auto-discover-table').innerHTML = nodes.map(function(n) {
      var badge;
      if (n.status === 'succeeded') badge = '<span class="badge success">onboarded</span>';
      else if (n.status === 'skipped_no_ip') badge = '<span class="badge pending">no IP</span>';
      else if (n.status === 'skipped_known') badge = '<span class="badge">already known</span>';
      else badge = '<span class="badge failed">' + escHtml(n.status) + '</span>';
      return '<tr><td><strong>' + escHtml(n.name) + '</strong></td><td><code>' + escHtml(n.ip)
        + '</code></td><td>' + escHtml(n.parent_node) + '</td><td>' + escHtml(n.hop_depth)
        + '</td><td>' + badge + '</td></tr>';
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
    function inferCidr(ip) {
      var parts = ip.split('.');
      return parts.length === 4 ? parts[0] + '.' + parts[1] + '.' + parts[2] + '.0/24' : ip + '/32';
    }
    document.getElementById('route-advisories-table').innerHTML = advisories.slice(0, 50).map(function(a) {
      return '<tr><td>' + escHtml(a.node_name) + '</td><td><code>' + escHtml(a.prefix) + '</code></td>'
        + '<td><code>' + escHtml(a.next_hop) + '</code></td><td>' + escHtml(a.protocol) + '</td>'
        + '<td>' + escHtml(a.vrf) + '</td>'
        + '<td><a href="/discover?cidr=' + encodeURIComponent(inferCidr(a.next_hop)) + '" class="btn small">Add to sweep</a></td></tr>';
    }).join('');
  } catch (e) { console.error('Route advisory load failed:', e); }
}

// ---------------------------------------------------------------------------
// Database Maintenance
// ---------------------------------------------------------------------------

async function loadMaintenance() {
  try {
    var r = await fetch('/api/admin/maintenance');
    if (!r.ok) return;
    var d = await r.json();
    if (!d.db_ready) return;
    var stats = d.stats || {};
    document.getElementById('maint-endpoint-rows').textContent = stats.endpoint_rows != null ? stats.endpoint_rows : '-';
    document.getElementById('maint-event-rows').textContent = stats.event_rows != null ? stats.event_rows : '-';
    document.getElementById('maint-observation-rows').textContent = stats.observation_rows != null ? stats.observation_rows : '-';
    document.getElementById('maint-watch-rows').textContent = stats.watch_rows != null ? stats.watch_rows : '-';
    document.getElementById('maint-route-rows').textContent = stats.route_rows != null ? stats.route_rows : '-';
    document.getElementById('maint-bgp-rows').textContent = stats.bgp_neighbor_rows != null ? stats.bgp_neighbor_rows : '-';
    document.getElementById('maintenance-retention').textContent = 'retention: ' + d.retention_days + ' days';
    var lastRunEl = document.getElementById('maint-last-run');
    if (d.last_run) {
      var sum = d.last_summary || {};
      lastRunEl.textContent = 'Last prune ' + timeAgo(d.last_run) + ' \u2014 '
        + (sum.events || 0) + ' events, ' + (sum.observations || 0) + ' obs, '
        + (sum.watches || 0) + ' watches';
    } else {
      lastRunEl.textContent = 'Last prune: never';
    }
    var meta = [];
    if (stats.oldest_event) meta.push('Oldest event: ' + new Date(stats.oldest_event).toLocaleString());
    document.getElementById('maint-meta').textContent = meta.join(' \u00b7 ');
  } catch (e) { console.error('Maintenance load failed:', e); }
}

// ---------------------------------------------------------------------------
// Event handlers
// ---------------------------------------------------------------------------

document.getElementById('logout-link').addEventListener('click', async function(e) {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
});

// ---------------------------------------------------------------------------
// Operations buttons with job-aware state
// ---------------------------------------------------------------------------

// Poll /api/jobs to update button states and ops status line
async function updateJobStates() {
  try {
    var resp = await fetch('/api/jobs');
    if (!resp.ok) return;
    var data = await resp.json();
    var jobs = data.jobs || [];
    var parts = [];
    jobs.forEach(function(j) {
      if (j.id === 'sweep') {
        var btn = document.getElementById('run-sweep-btn');
        if (j.running) {
          btn.disabled = true; btn.textContent = 'Sweep...';
          parts.push('Sweep running');
        } else {
          btn.disabled = false; btn.textContent = 'Run Sweep';
          if (j.last_run) parts.push('Sweep ' + timeAgo(j.last_run));
        }
        var sweepEl = document.getElementById('sweep-status');
        if (sweepEl) {
          sweepEl.textContent = j.running ? 'Sweep in progress...' : (j.last_run ? 'Last sweep: ' + timeAgo(j.last_run) : '');
        }
      }
      if (j.id === 'modular_poller') {
        var btn = document.getElementById('trigger-collection-btn');
        if (j.running) {
          parts.push('Poller active');
        } else if (j.last_run) {
          parts.push('Polled ' + timeAgo(j.last_run));
        }
      }
    });
    var opsEl = document.getElementById('ops-status');
    if (opsEl) opsEl.textContent = parts.join(' · ');
  } catch (e) { /* degrade gracefully */ }
}

// Sync Network Data — fires and shows status underneath
document.getElementById('sync-all-btn').addEventListener('click', async function() {
  var btn = this; var opsEl = document.getElementById('ops-status');
  btn.disabled = true; btn.textContent = 'Sync...';
  opsEl.textContent = 'Submitting sync job...';
  try {
    var resp = await fetch('/api/nautobot/sync-network-data', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ sync_cables: true }),
    });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    opsEl.textContent = resp.ok ? 'Sync job submitted' : 'Sync failed';
  } catch (e) { opsEl.textContent = 'Sync failed: ' + e.message; }
  btn.textContent = 'Sync Network Data'; btn.disabled = false;
  setTimeout(updateJobStates, 5000);
});

// Trigger Collection — fires and shows status underneath
document.getElementById('trigger-collection-btn').addEventListener('click', async function() {
  var btn = this; var opsEl = document.getElementById('ops-status');
  btn.disabled = true; btn.textContent = 'Collect...';
  opsEl.textContent = 'Triggering collection...';
  try {
    var resp = await fetch('/api/endpoints/collect', { method: 'POST' });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    opsEl.textContent = resp.ok ? 'Collection triggered' : 'Already running';
  } catch (e) { opsEl.textContent = 'Collection failed'; }
  btn.textContent = 'Collect Endpoints'; btn.disabled = false;
  setTimeout(updateJobStates, 3000);
});

// Run Sweep — fires and shows status underneath
document.getElementById('run-sweep-btn').addEventListener('click', async function() {
  var btn = this; var opsEl = document.getElementById('ops-status');
  btn.disabled = true; btn.textContent = 'Sweep...';
  opsEl.textContent = 'Starting sweep...';
  try {
    var resp = await fetch('/api/discover/sweep-scheduled', { method: 'POST' });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    var data = await resp.json();
    if (resp.ok) {
      opsEl.textContent = 'Sweep started: ' + (data.cidr_ranges || []).join(', ');
    } else {
      opsEl.textContent = data.detail || 'No sweep schedule configured';
      btn.disabled = false; btn.textContent = 'Run Sweep';
    }
  } catch (e) { opsEl.textContent = 'Sweep failed'; btn.disabled = false; btn.textContent = 'Run Sweep'; }
  setTimeout(updateJobStates, 3000);
});

// Discover field — navigates to discovery page with CIDR pre-filled
document.getElementById('quick-discover-btn').addEventListener('click', function() {
  var val = document.getElementById('quick-discover-input').value.trim();
  if (!val) return;
  window.location.href = '/discover?cidr=' + encodeURIComponent(val);
});

// Incomplete sync + exclude (delegation)
document.addEventListener('click', async function(ev) {
  var t = ev.target;
  if (t && t.classList && t.classList.contains('exclude-device-btn')) {
    var name = t.getAttribute('data-name');
    if (!confirm('Exclude device "' + name + '" from advisories?')) return;
    var reason = prompt('Reason? (optional)') || '';
    try {
      var r = await fetch('/api/discover/excludes', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ identifier: name, type: 'device_name', reason: reason }),
      });
      if (!r.ok) { alert('Failed to exclude'); return; }
      loadIncompleteDevices();
    } catch (e) { alert('Failed: ' + e); }
    return;
  }
  if (t && t.id === 'sync-incomplete-btn') {
    t.disabled = true; t.textContent = 'Syncing...';
    incompleteSyncObserved = false;
    document.getElementById('sync-incomplete-progress').style.display = 'block';
    try {
      await fetch('/api/discover/sync-incomplete', { method: 'POST' });
      if (!incompleteSyncPoll) incompleteSyncPoll = setInterval(pollSyncIncomplete, 1500);
    } catch (e) { t.disabled = false; t.textContent = 'Sync All'; }
  }
  if (t && t.id === 'maint-preview-btn') {
    t.disabled = true; t.textContent = 'Previewing...';
    try {
      var r = await fetch('/api/admin/prune/preview');
      var d = await r.json();
      var wp = d.would_prune || {};
      var el = document.getElementById('maint-preview-result');
      el.className = 'alert'; el.style.display = 'block';
      el.textContent = 'Would delete: ' + (wp.events || 0) + ' events, ' + (wp.observations || 0)
        + ' observations, ' + (wp.watches || 0) + ' watches, ' + (wp.sentinels || 0) + ' sentinels';
    } catch (e) { console.error('Preview failed:', e); }
    t.disabled = false; t.textContent = 'Preview Prune';
  }
  if (t && t.id === 'maint-prune-btn') {
    if (!confirm('Run database prune now?')) return;
    t.disabled = true; t.textContent = 'Pruning...';
    try {
      var r = await fetch('/api/admin/prune', { method: 'POST' });
      if (!r.ok) { alert('Prune failed'); return; }
      var d = await r.json();
      var p = d.pruned || {};
      var el = document.getElementById('maint-preview-result');
      el.className = 'alert success'; el.style.display = 'block';
      el.textContent = 'Pruned: ' + (p.events || 0) + ' events, ' + (p.observations || 0) + ' obs';
      loadMaintenance();
    } catch (e) { alert('Prune failed: ' + e); }
    t.disabled = false; t.textContent = 'Run Prune Now';
  }
});

// ---------------------------------------------------------------------------
// Theme + preferences
// ---------------------------------------------------------------------------

function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('mnm-theme', theme);
  document.querySelectorAll('.theme-switcher button').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-theme') === theme);
  });
}

(function() {
  var saved = localStorage.getItem('mnm-theme') || 'dark';
  setTheme(saved);
  document.querySelectorAll('.theme-switcher button').forEach(function(btn) {
    btn.addEventListener('click', function() { setTheme(btn.getAttribute('data-theme')); });
  });
})();

MNMPreferences.init();

// ---------------------------------------------------------------------------
// Auto-refresh
// ---------------------------------------------------------------------------

var refreshTimer = null;

function startAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  var interval = MNMPreferences.get('autoRefreshInterval');
  if (interval && interval > 0) {
    refreshTimer = setInterval(function() {
      loadContainers();
      loadPolling();
      loadInterfaceErrors();
      loadSystemHealth();
      updateJobStates();
    }, interval * 1000);
  }
}
window.addEventListener('mnm-preferences-changed', startAutoRefresh);

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

checkAuth().then(function() {
  return MNMServiceURLs.load();
}).then(function() {
  initServiceLinks();
  loadContainers();
  loadPolling();
  loadSystemHealth();
  loadInterfaceErrors();
  updateJobStates();
  loadRecentEvents();
  loadProxmox();
  loadNeighbors();
  loadIncompleteDevices();
  loadSubnets();
  loadRouteAdvisories();
  loadAutoDiscovered();
  loadMaintenance();
  startAutoRefresh();
});
