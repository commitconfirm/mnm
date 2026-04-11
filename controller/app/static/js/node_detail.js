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
    { key: 'vlan', label: 'VLAN', sortable: true, render: function(v) { return v != null ? esc(String(v)) : '-'; } },
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

// Load data
async function loadNodeInfo() {
  try {
    var r = await fetch('/api/nodes/' + encodeURIComponent(nodeName));
    if (!r.ok) return;
    var d = await r.json();
    var dev = d.device || {};
    document.getElementById('node-title').textContent = nodeName;

    var platform = (dev.platform || {}).display || '';
    var location = (dev.location || {}).display || '';
    var role = ((dev.role || dev.device_role || {}).display) || '';
    var pip = (dev.primary_ip4 || {}).display || (dev.primary_ip4 || {}).address || '';

    var meta = '';
    if (pip) meta += '<div class="field"><div class="field-label">Primary IP</div><div class="field-value">' + esc(pip) + '</div></div>';
    if (platform) meta += '<div class="field"><div class="field-label">Platform</div><div class="field-value">' + esc(platform) + '</div></div>';
    if (role) meta += '<div class="field"><div class="field-label">Role</div><div class="field-value">' + esc(role) + '</div></div>';
    if (location) meta += '<div class="field"><div class="field-label">Location</div><div class="field-value">' + esc(location) + '</div></div>';
    document.getElementById('node-meta').innerHTML = meta;

    if (dev.url) {
      var link = document.getElementById('nautobot-link');
      link.href = dev.url;
      link.style.display = 'inline-flex';
    }

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
  } catch (e) { console.error('Failed to load node info:', e); }
}

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

function loadAll() {
  loadNodeInfo();
  loadArp();
  loadMac();
  loadRoutes();
  loadLldp();
  loadBgp();
}

checkAuth().then(loadAll);
