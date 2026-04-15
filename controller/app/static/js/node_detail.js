/* MNM Node Detail — per-node tabbed data view */

var nodeName = decodeURIComponent(window.location.pathname.split('/nodes/')[1] || '');
var allArp = [], allMac = [], allRoutes = [];

function esc(s) { if (!s) return ''; var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function timeAgo(iso) {
  if (!iso) return '-';
  var d = Date.now() - new Date(iso).getTime();
  if (d < 0) return 'just now';
  var m = Math.floor(d / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + 'm ago';
  var h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago';
  return Math.floor(h / 24) + 'd ago';
}

// DataTables
var arpDT = new DataTable({
  containerId: 'arp-dt',
  columns: [
    { key: 'ip', label: 'IP', sortable: true, render: function(v) { return esc(v); } },
    { key: 'mac', label: 'MAC', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
    { key: 'interface', label: 'Interface', sortable: true, render: function(v) { return esc(v); } },
    { key: 'vrf', label: 'VRF', sortable: true, render: function(v) { return esc(v); } },
    { key: 'collected_at', label: 'Last Seen', sortable: true, render: function(v) { return '<span title="' + esc(v) + '">' + timeAgo(v) + '</span>'; } },
  ],
  pageSize: 100, storageKey: 'mnm-node-arp',
});

var macDT = new DataTable({
  containerId: 'mac-dt',
  columns: [
    { key: 'mac', label: 'MAC', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
    { key: 'interface', label: 'Interface', sortable: true, render: function(v) { return esc(v); } },
    { key: 'vlan', label: 'VLAN', sortable: true, render: function(v) { return (v != null && v !== 0 && v !== '0') ? esc(String(v)) : '-'; } },
    { key: 'entry_type', label: 'Type', sortable: true, render: function(v) { return '<span class="badge ' + esc(v) + '">' + esc(v) + '</span>'; } },
    { key: 'collected_at', label: 'Last Seen', sortable: true, render: function(v) { return '<span title="' + esc(v) + '">' + timeAgo(v) + '</span>'; } },
  ],
  pageSize: 100, storageKey: 'mnm-node-mac',
});

var routesDT = new DataTable({
  containerId: 'routes-dt',
  columns: [
    { key: 'prefix', label: 'Prefix', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
    { key: 'next_hop', label: 'Next Hop', sortable: true, render: function(v) { return v ? esc(v) : '<span style="color:var(--text-muted)">connected</span>'; } },
    { key: 'protocol', label: 'Protocol', sortable: true, render: function(v) { return '<span class="badge">' + esc(v) + '</span>'; } },
    { key: 'metric', label: 'Metric', sortable: true, render: function(v) { return v != null ? esc(String(v)) : '-'; } },
    { key: 'vrf', label: 'VRF', sortable: true, render: function(v) { return esc(v); } },
    { key: 'collected_at', label: 'Collected', sortable: true, render: function(v) { return '<span title="' + esc(v) + '">' + timeAgo(v) + '</span>'; } },
  ],
  pageSize: 100, storageKey: 'mnm-node-routes',
});

var lldpDT = new DataTable({
  containerId: 'lldp-dt',
  columns: [
    { key: 'local_interface', label: 'Local Interface', sortable: true, render: function(v) { return esc(v); } },
    { key: 'remote_system_name', label: 'Remote System', sortable: true, render: function(v) { return '<strong>' + esc(v) + '</strong>'; } },
    { key: 'remote_port', label: 'Remote Port', sortable: true, render: function(v) { return esc(v); } },
    { key: 'remote_chassis_id', label: 'Chassis ID', sortable: true, render: function(v) { return v ? '<code>' + esc(v) + '</code>' : '-'; } },
    { key: 'collected_at', label: 'Last Seen', sortable: true, render: function(v) { return '<span title="' + esc(v) + '">' + timeAgo(v) + '</span>'; } },
  ],
  pageSize: 50, storageKey: 'mnm-node-lldp',
});

var bgpDT = new DataTable({
  containerId: 'bgp-dt',
  columns: [
    { key: 'neighbor_ip', label: 'Neighbor', sortable: true, render: function(v) { return esc(v); } },
    { key: 'remote_asn', label: 'Remote AS', sortable: true, render: function(v) { return esc(String(v || '')); } },
    { key: 'state', label: 'State', sortable: true, render: function(v) {
      var cls = v === 'Established' ? 'green' : 'red';
      return '<span class="dot-wrap"><span class="dot ' + cls + '" style="width:8px;height:8px"></span></span> ' + esc(v);
    }},
    { key: 'prefixes_received', label: 'Rx Prefixes', sortable: true, render: function(v) { return v != null ? String(v) : '-'; } },
    { key: 'prefixes_sent', label: 'Tx Prefixes', sortable: true, render: function(v) { return v != null ? String(v) : '-'; } },
    { key: 'vrf', label: 'VRF', sortable: true, render: function(v) { return esc(v); } },
    { key: 'collected_at', label: 'Collected', sortable: true, render: function(v) { return '<span title="' + esc(v) + '">' + timeAgo(v) + '</span>'; } },
  ],
  pageSize: 50, storageKey: 'mnm-node-bgp',
});

// Tab switching
document.getElementById('tab-bar').addEventListener('click', function(e) {
  var btn = e.target.closest('button');
  if (!btn) return;
  var tab = btn.getAttribute('data-tab');
  document.querySelectorAll('.tab-bar button').forEach(function(b) { b.classList.remove('active'); });
  document.querySelectorAll('.tab-panel').forEach(function(p) { p.classList.remove('active'); });
  btn.classList.add('active');
  document.getElementById('panel-' + tab).classList.add('active');
});

// Helpers
function fmtBytes(n) {
  if (!n || n <= 0) return '-';
  var units = ['B','KB','MB','GB','TB'];
  var i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 ? 1 : 0) + ' ' + units[i];
}

// Physical interface name prefixes (for "show all" filter)
// Logical/internal interface prefixes to hide by default.
// Everything NOT matching this list is shown (physical, aggregate, etc.).
var _LOGICAL_PREFIXES = [
  'lo0','loopback','lsi','dsc','gre','ipip','mtun','pimd','pime','tap',
  'vtep','jsrv','rbeb','bme','pp0','ppd','ppe','fti','irb','vlan',
  'st0','tunnel','sdwan','lsq-','lt-','mt-','sp-','ip-','gr-',
  'pfe-','pfh-','esi',
];

function isLogicalInterface(name) {
  var lower = name.toLowerCase();
  // Exact matches
  if (_LOGICAL_PREFIXES.indexOf(lower) !== -1) return true;
  // Prefix matches (with subinterface: lo0.0, irb.140, etc.)
  for (var i = 0; i < _LOGICAL_PREFIXES.length; i++) {
    if (lower.startsWith(_LOGICAL_PREFIXES[i] + '.') || lower === _LOGICAL_PREFIXES[i]) return true;
  }
  return false;
}

// Load device info header
async function loadNodeInfo() {
  try {
    // Load both /info (NAPALM facts) and /nodes/{name} (poll jobs) in parallel
    var [infoResp, nodeResp] = await Promise.all([
      fetch('/api/nodes/' + encodeURIComponent(nodeName) + '/info'),
      fetch('/api/nodes/' + encodeURIComponent(nodeName)),
    ]);

    document.getElementById('node-title').textContent = nodeName;

    if (infoResp.ok) {
      var info = await infoResp.json();
      var fields = [
        { l: 'Primary IP', v: (info.primary_ip || '').replace(/\/\d+$/, '') },
        { l: 'Platform', v: info.platform },
        { l: 'Model', v: info.model },
        { l: 'Serial', v: info.serial_number },
        { l: 'OS Version', v: info.os_version },
        { l: 'Uptime', v: info.uptime },
        { l: 'Interfaces', v: info.interfaces_up + '/' + info.interface_count + ' up' },
        { l: 'Role', v: info.role },
        { l: 'Location', v: info.location },
      ];
      document.getElementById('node-meta').innerHTML = fields.filter(function(f) { return f.v; }).map(function(f) {
        return '<div class="field"><div class="field-label">' + esc(f.l) + '</div><div class="field-value">' + esc(String(f.v)) + '</div></div>';
      }).join('');

      if (info.device_id) {
        var link = document.getElementById('nautobot-link');
        link.href = MNMServiceURLs.nautobotDevice(info.device_id);
        link.style.display = 'inline-flex';
      }
    }

    if (nodeResp.ok) {
      var d = await nodeResp.json();
      // Poll history
      var jobs = d.jobs || {};
      var tbody = document.getElementById('polls-tbody');
      var jobTypes = Object.keys(jobs).sort();
      document.getElementById('count-polls').textContent = jobTypes.length;
      tbody.innerHTML = jobTypes.map(function(jt) {
        var j = jobs[jt];
        var dot = j.last_error && !j.last_success ? 'red' : (j.last_success ? 'green' : 'grey');
        return '<tr><td><strong>' + esc(jt) + '</strong></td>'
          + '<td><span class="dot ' + dot + '" style="width:8px;height:8px"></span></td>'
          + '<td>' + (j.last_success ? timeAgo(j.last_success) : '-') + '</td>'
          + '<td style="font-size:0.8rem;color:var(--red)">' + esc(j.last_error || '') + '</td>'
          + '<td>' + (j.last_duration != null ? j.last_duration + 's' : '-') + '</td>'
          + '<td>' + j.interval_sec + 's</td></tr>';
      }).join('');
    }
  } catch (e) { console.error('Failed to load node info:', e); }
}

// Interfaces DataTable
var allIfaces = [];
var ifaceDT = new DataTable({
  containerId: 'iface-dt',
  columns: [
    { key: 'health', label: '', sortable: true, render: function(v) {
      return '<span class="dot ' + (v || 'grey') + '" style="width:10px;height:10px"></span>';
    }},
    { key: 'name', label: 'Interface', sortable: true, render: function(v) { return '<strong>' + esc(v) + '</strong>'; } },
    { key: 'description', label: 'Description', sortable: true, render: function(v) { return esc(v || ''); } },
    { key: 'is_up', label: 'Status', sortable: true, render: function(v, row) {
      if (!row.is_enabled) return '<span style="color:var(--text-muted)">Admin Down</span>';
      return v ? '<span style="color:var(--green)">Up</span>' : '<span style="color:var(--red)">Down</span>';
    }},
    { key: 'speed_display', label: 'Speed', sortable: true },
    { key: 'mtu', label: 'MTU', sortable: true, render: function(v) { return v || '-'; } },
    { key: 'errors_in', label: 'Err In', sortable: true, render: function(v) {
      return v > 0 ? '<span class="counter-error">' + v + '</span>' : '0';
    }},
    { key: 'errors_out', label: 'Err Out', sortable: true, render: function(v) {
      return v > 0 ? '<span class="counter-error">' + v + '</span>' : '0';
    }},
    { key: 'discards_in', label: 'Disc In', sortable: true, render: function(v) {
      return v > 0 ? '<span class="counter-discard">' + v + '</span>' : '0';
    }},
    { key: 'discards_out', label: 'Disc Out', sortable: true, render: function(v) {
      return v > 0 ? '<span class="counter-discard">' + v + '</span>' : '0';
    }},
    { key: 'octets_in', label: 'Traffic In', sortable: true, render: function(v) { return fmtBytes(v); } },
    { key: 'octets_out', label: 'Traffic Out', sortable: true, render: function(v) { return fmtBytes(v); } },
  ],
  pageSize: 100, storageKey: 'mnm-node-interfaces',
});

var _ifaceRetries = 0;
async function loadInterfaces() {
  try {
    if (_ifaceRetries === 0) {
      document.getElementById('count-interfaces').textContent = '...';
      document.getElementById('iface-dt').innerHTML = '<div class="loading-indicator">Loading interfaces from device...</div>';
    }
    var r = await fetch('/api/nodes/' + encodeURIComponent(nodeName) + '/interfaces');
    if (!r.ok) {
      document.getElementById('iface-dt').innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-muted)">Failed to load interfaces — device may be unreachable</div>';
      return;
    }
    allIfaces = await r.json();
    if (!allIfaces.length && _ifaceRetries < 12) {
      // Server is fetching data in background — retry
      _ifaceRetries++;
      document.getElementById('iface-dt').innerHTML = '<div class="loading-indicator">Querying device via NAPALM (attempt ' + _ifaceRetries + ')...</div>';
      setTimeout(loadInterfaces, 5000);
      return;
    }
    _ifaceRetries = 0;
    document.getElementById('count-interfaces').textContent = allIfaces.length;
    if (!allIfaces.length) {
      document.getElementById('iface-dt').innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-muted)">No interface data available</div>';
      return;
    }
    applyIfaceFilter();
  } catch (e) {
    console.error('Failed to load interfaces:', e);
    document.getElementById('iface-dt').innerHTML = '<div style="text-align:center;padding:24px;color:var(--text-muted)">Error loading interfaces</div>';
  }
}

function applyIfaceFilter() {
  var q = (document.getElementById('iface-search').value || '').toLowerCase();
  var showAll = document.getElementById('iface-show-all').checked;
  ifaceDT.setFilter(function(r) {
    if (!showAll && isLogicalInterface(r.name)) return false;
    if (q && (r.name || '').toLowerCase().indexOf(q) === -1 && (r.description || '').toLowerCase().indexOf(q) === -1) return false;
    return true;
  });
  ifaceDT.setData(allIfaces);
  ifaceDT.render();
}

document.getElementById('iface-search').addEventListener('input', applyIfaceFilter);
document.getElementById('iface-show-all').addEventListener('change', applyIfaceFilter);

async function loadArp() {
  try {
    var r = await fetch('/api/nodes/' + encodeURIComponent(nodeName) + '/arp');
    if (!r.ok) return;
    var d = await r.json();
    allArp = d.entries || [];
    document.getElementById('count-arp').textContent = allArp.length;
    arpDT.setData(allArp); arpDT.render();
  } catch (e) { console.error('Failed to load ARP:', e); }
}

async function loadMac() {
  try {
    var r = await fetch('/api/nodes/' + encodeURIComponent(nodeName) + '/mac-table');
    if (!r.ok) return;
    var d = await r.json();
    allMac = d.entries || [];
    document.getElementById('count-mac').textContent = allMac.length;
    macDT.setData(allMac); macDT.render();
  } catch (e) { console.error('Failed to load MAC:', e); }
}

async function loadRoutes() {
  try {
    var r = await fetch('/api/routes/' + encodeURIComponent(nodeName));
    if (!r.ok) return;
    var d = await r.json();
    allRoutes = d.routes || [];
    document.getElementById('count-routes').textContent = allRoutes.length;
    routesDT.setData(allRoutes); routesDT.render();
  } catch (e) { console.error('Failed to load routes:', e); }
}

async function loadLldp() {
  try {
    var r = await fetch('/api/nodes/' + encodeURIComponent(nodeName) + '/lldp');
    if (!r.ok) return;
    var d = await r.json();
    var entries = d.entries || [];
    document.getElementById('count-lldp').textContent = entries.length;
    lldpDT.setData(entries); lldpDT.render();
  } catch (e) { console.error('Failed to load LLDP:', e); }
}

async function loadBgp() {
  try {
    var r = await fetch('/api/bgp/' + encodeURIComponent(nodeName));
    if (!r.ok) return;
    var d = await r.json();
    var entries = d.neighbors || [];
    document.getElementById('count-bgp').textContent = entries.length;
    bgpDT.setData(entries); bgpDT.render();
  } catch (e) { console.error('Failed to load BGP:', e); }
}

// Search filters
document.getElementById('arp-search').addEventListener('input', function() {
  var q = this.value.toLowerCase();
  arpDT.setFilter(function(r) {
    return !q || (r.ip || '').toLowerCase().includes(q) || (r.mac || '').toLowerCase().includes(q);
  });
  arpDT.render();
});
document.getElementById('mac-search').addEventListener('input', function() {
  var q = this.value.toLowerCase();
  macDT.setFilter(function(r) {
    return !q || (r.mac || '').toLowerCase().includes(q) || (r.interface || '').toLowerCase().includes(q);
  });
  macDT.render();
});
document.getElementById('routes-search').addEventListener('input', function() {
  var q = this.value.toLowerCase();
  routesDT.setFilter(function(r) {
    return !q || (r.prefix || '').toLowerCase().includes(q)
      || (r.next_hop || '').toLowerCase().includes(q)
      || (r.protocol || '').toLowerCase().includes(q);
  });
  routesDT.render();
});

// Poll Now
document.getElementById('poll-now-btn').addEventListener('click', async function() {
  var btn = this;
  btn.disabled = true; btn.textContent = 'Polling...';
  try {
    await fetch('/api/polling/trigger/' + encodeURIComponent(nodeName), { method: 'POST' });
    btn.textContent = 'Triggered';
    setTimeout(function() { loadAll(); btn.textContent = 'Poll Now'; btn.disabled = false; }, 5000);
  } catch (e) { btn.textContent = 'Failed'; setTimeout(function() { btn.textContent = 'Poll Now'; btn.disabled = false; }, 2000); }
});

// Logout + theme
document.getElementById('logout-link').addEventListener('click', async function(e) {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
});
function setTheme(t) { document.documentElement.setAttribute('data-theme', t); localStorage.setItem('mnm-theme', t); document.querySelectorAll('.theme-switcher button').forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-theme') === t); }); }
(function() { var s = localStorage.getItem('mnm-theme') || 'dark'; setTheme(s); document.querySelectorAll('.theme-switcher button').forEach(function(b) { b.addEventListener('click', function() { setTheme(b.getAttribute('data-theme')); }); }); })();
MNMPreferences.init();

// Auth + init
async function checkAuth() {
  var r = await fetch('/api/auth/check');
  var d = await r.json();
  if (!d.authenticated) window.location.href = '/login';
}

async function loadAll() {
  // Load info first (warms NAPALM cache), then interfaces reuses cached data.
  // DB-backed tabs (ARP, MAC, routes, etc.) load in parallel — they're fast.
  loadArp();
  loadMac();
  loadRoutes();
  loadLldp();
  loadBgp();
  loadComments();
  loadChangeHistory();
  await loadNodeInfo();
  await loadInterfaces();
}

// ---- Comments ----
async function loadComments() {
  try {
    var r = await fetch('/api/comments?target_type=node&target_id=' + encodeURIComponent(nodeName));
    if (!r.ok) return;
    var comments = await r.json();
    document.getElementById('comment-count').textContent = comments.length ? '(' + comments.length + ')' : '';
    var list = document.getElementById('comment-list');
    if (!comments.length) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem;padding:8px 0">No comments yet.</div>';
      return;
    }
    list.innerHTML = comments.map(function(c) {
      var when = new Date(c.created_at).toLocaleString();
      return '<div style="padding:10px 12px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:8px">'
        + '<div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-muted);margin-bottom:4px">'
        + '<span><strong>' + esc(c.created_by) + '</strong> &middot; ' + esc(when) + '</span>'
        + '<button class="btn small" style="padding:2px 8px;font-size:0.72rem" data-comment-id="' + esc(c.id) + '">Delete</button>'
        + '</div>'
        + '<div style="white-space:pre-wrap">' + esc(c.comment_text) + '</div>'
        + '</div>';
    }).join('');
  } catch (e) { console.error('Failed to load comments:', e); }
}

async function submitComment() {
  var input = document.getElementById('comment-input');
  var text = input.value.trim();
  if (!text) return;
  var btn = document.getElementById('comment-submit');
  btn.disabled = true;
  try {
    var r = await fetch('/api/comments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_type: 'node', target_id: nodeName, comment_text: text }),
    });
    if (!r.ok) { alert('Failed to add comment'); return; }
    input.value = '';
    await loadComments();
    await loadChangeHistory();
  } catch (e) { alert('Failed: ' + e.message); }
  btn.disabled = false;
}

async function deleteComment(commentId) {
  if (!confirm('Delete this comment?')) return;
  try {
    var r = await fetch('/api/comments/' + encodeURIComponent(commentId), { method: 'DELETE' });
    if (r.status !== 204) { alert('Failed to delete'); return; }
    await loadComments();
    await loadChangeHistory();
  } catch (e) { alert('Failed: ' + e.message); }
}

// ---- Change History ----
var historyDT = null;

async function loadChangeHistory() {
  try {
    var r = await fetch('/api/history?target_type=node&target_id=' + encodeURIComponent(nodeName) + '&limit=200');
    if (!r.ok) return;
    var history = await r.json();
    document.getElementById('history-count').textContent = history.length ? '(' + history.length + ')' : '(none)';
    if (!historyDT) {
      historyDT = new DataTable({
        containerId: 'history-dt',
        columns: [
          { key: 'changed_at', label: 'When', sortable: true, render: function(v) { return '<span title="' + esc(v) + '">' + new Date(v).toLocaleString() + '</span>'; } },
          { key: 'field_name', label: 'Field', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
          { key: 'old_value', label: 'Old', sortable: false, render: function(v) { return v == null ? '<span style="color:var(--text-muted)">—</span>' : esc(v); } },
          { key: 'new_value', label: 'New', sortable: false, render: function(v) { return v == null ? '<span style="color:var(--text-muted)">—</span>' : esc(v); } },
          { key: 'change_source', label: 'Source', sortable: true, render: function(v) { return '<span class="badge">' + esc(v) + '</span>'; } },
        ],
        pageSize: 25,
        storageKey: 'mnm-node-history',
      });
    }
    historyDT.setData(history);
    historyDT.render();
  } catch (e) { console.error('Failed to load change history:', e); }
}

// Wire up comment submit + history toggle + delete (delegation)
document.getElementById('comment-submit').addEventListener('click', submitComment);
document.getElementById('history-toggle').addEventListener('click', function() {
  var container = document.getElementById('history-container');
  var btn = this;
  if (container.style.display === 'none') {
    container.style.display = '';
    btn.innerHTML = btn.innerHTML.replace('&#9658;', '&#9660;');
  } else {
    container.style.display = 'none';
    btn.innerHTML = btn.innerHTML.replace('&#9660;', '&#9658;');
  }
});
document.getElementById('comment-list').addEventListener('click', function(e) {
  var btn = e.target.closest('[data-comment-id]');
  if (btn) deleteComment(btn.getAttribute('data-comment-id'));
});

checkAuth().then(function() { return MNMServiceURLs.load(); }).then(loadAll);
