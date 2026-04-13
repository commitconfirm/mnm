/* MNM Investigations — unified network search (Phase 2.9) */

function esc(s) { if (!s && s !== 0) return ''; var d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }
function timeAgo(iso) {
  if (!iso) return '-';
  var d = Date.now() - new Date(iso).getTime();
  if (d < 0) return 'just now';
  var m = Math.floor(d / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + 'm ago';
  var h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago'; return Math.floor(h / 24) + 'd ago';
}
function macLink(mac) { return '<a href="/endpoints/' + encodeURIComponent(mac) + '"><code>' + esc(mac) + '</code></a>'; }
function nodeLink(name) { return '<a href="/nodes/' + encodeURIComponent(name) + '">' + esc(name) + '</a>'; }

// ---- Create a DataTable, set data, and render ----
function makeTable(containerId, columns, storageKey, data) {
  var el = document.getElementById(containerId);
  if (el) el.innerHTML = '';
  var t = new DataTable({
    containerId: containerId,
    columns: columns,
    storageKey: storageKey,
    pageSize: 50,
  });
  t.setData(data);
  t.render();
  return t;
}

// ---- Populate node filter dropdown ----
(async function loadNodes() {
  try {
    var r = await fetch('/api/nodes');
    if (!r.ok) return;
    var d = await r.json();
    var sel = document.getElementById('filter-node');
    (d.nodes || []).forEach(function(n) {
      var opt = document.createElement('option');
      opt.value = n.name;
      opt.textContent = n.name;
      sel.appendChild(opt);
    });
  } catch (e) {}
})();

// ---- Hide all result sections ----
function hideAll() {
  var ids = ['query-info', 'section-location', 'section-vm', 'section-endpoint',
             'section-endpoints', 'section-arp', 'section-mac', 'section-routes',
             'section-fib', 'section-gateways', 'section-lldp', 'no-results'];
  ids.forEach(function(id) { document.getElementById(id).style.display = 'none'; });
}

// ---- Main search function ----
async function doSearch() {
  var q = document.getElementById('search-input').value.trim();
  if (!q) return;

  var node = document.getElementById('filter-node').value;
  var vlan = document.getElementById('filter-vlan').value.trim();
  var type = document.getElementById('filter-type').value;

  // Update URL
  var params = new URLSearchParams({ q: q });
  if (node) params.set('node', node);
  if (vlan) params.set('vlan', vlan);
  if (type) params.set('type', type);
  history.replaceState(null, '', '/investigate?' + params.toString());

  document.getElementById('search-results').style.display = 'block';
  hideAll();

  var info = document.getElementById('query-info');
  info.style.display = '';
  info.innerHTML = 'Searching...';

  try {
    var url = '/api/investigate?' + params.toString();
    var r = await fetch(url);
    if (!r.ok) { info.innerHTML = 'Search failed (HTTP ' + r.status + ')'; return; }
    var d = await r.json();
    var res = d.results || {};
    var hasResults = false;

    info.innerHTML = 'Query: <strong>' + esc(d.query) + '</strong> &mdash; type: <span class="query-type">' + esc(d.query_type) + '</span>';

    // Location card (MAC search)
    if (res.location) {
      hasResults = true;
      document.getElementById('section-location').style.display = '';
      var loc = res.location;
      document.getElementById('location-content').innerHTML =
        '<div class="location-label">Physical Location</div>' +
        '<div class="location-text">' + esc(loc.description) + '</div>';
    }

    // VM host card (MAC search)
    if (res.vm_host) {
      hasResults = true;
      document.getElementById('section-vm').style.display = '';
      var vm = res.vm_host;
      document.getElementById('vm-content').innerHTML =
        '<div><span class="vm-name">' + esc(vm.name) + '</span></div>' +
        '<div class="vm-detail">VMID ' + esc(vm.vmid) + ' on ' + esc(vm.node) +
        ' &mdash; ' + esc(vm.status) + ' (' + esc(vm.type) + ')</div>';
    }

    // Single endpoint record (MAC search)
    if (res.endpoint) {
      hasResults = true;
      var ep = res.endpoint;
      document.getElementById('section-endpoint').style.display = '';
      var allIps = (ep.all_ips || [ep.ip]).filter(Boolean);
      document.getElementById('endpoint-content').innerHTML =
        '<table><tbody>' +
        '<tr><td style="width:140px;color:var(--text-muted)">Type</td><td>' + MNMIcons.deviceIcon(ep.classification || 'unknown') + ' ' + esc(ep.classification || 'unknown') + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">MAC</td><td>' + macLink(ep.mac || ep.mac_address) + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">IP(s)</td><td>' + allIps.map(esc).join(', ') + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">Hostname</td><td>' + esc(ep.hostname) + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">Vendor</td><td>' + esc(ep.mac_vendor) + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">Switch / Port</td><td>' + (function(){ var sw = ep.device_name || ep.current_switch || ''; var pt = ep.switch_port || ep.current_port || ''; sw = (sw === '(none)') ? '' : sw; pt = (pt === '(none)') ? '' : pt; return (sw || pt) ? esc(sw) + (sw && pt ? ' / ' : '') + esc(pt) : '-'; })() + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">VLAN</td><td>' + (function(){ var v = ep.vlan || ep.current_vlan; return (v && v !== 0 && v !== '0') ? esc(String(v)) : '-'; })() + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">First Seen</td><td>' + esc(ep.first_seen) + '</td></tr>' +
        '<tr><td style="color:var(--text-muted)">Last Seen</td><td>' + esc(ep.last_seen) + '</td></tr>' +
        '</tbody></table>';
    }

    // Endpoint matches (IP/text search)
    if (res.endpoints && res.endpoints.length) {
      hasResults = true;
      document.getElementById('section-endpoints').style.display = '';
      document.getElementById('ep-count').textContent = '(' + res.endpoints.length + ')';
      makeTable('ep-table-wrap', [
        { key: 'classification', label: '', sortable: true, render: function(v) { return MNMIcons.deviceIcon(v || 'unknown', 'sm'); } },
        { key: 'mac', label: 'MAC', sortable: true, render: function(v) { return macLink(v); } },
        { key: 'ip', label: 'IP', sortable: true },
        { key: 'hostname', label: 'Hostname', sortable: true },
        { key: 'mac_vendor', label: 'Vendor', sortable: true },
        { key: 'device_name', label: 'Switch', sortable: true, render: function(v) { return (v && v !== '(none)') ? esc(v) : '-'; } },
        { key: 'switch_port', label: 'Port', sortable: true, render: function(v) { return (v && v !== '(none)') ? esc(v) : '-'; } },
        { key: 'vlan', label: 'VLAN', sortable: true, render: function(v) { return (v && v !== 0 && v !== '0') ? esc(String(v)) : '-'; } },
      ], 'investigate-endpoints', res.endpoints);
    }

    // ARP table
    if (res.arp_hits && res.arp_hits.length) {
      hasResults = true;
      document.getElementById('section-arp').style.display = '';
      document.getElementById('arp-count').textContent = '(' + res.arp_hits.length + ')';
      makeTable('arp-table-wrap', [
        { key: 'node_name', label: 'Node', sortable: true, render: function(v) { return nodeLink(v); } },
        { key: 'ip', label: 'IP', sortable: true },
        { key: 'mac', label: 'MAC', sortable: true, render: function(v) { return macLink(v); } },
        { key: 'interface', label: 'Interface', sortable: true },
        { key: 'vrf', label: 'VRF', sortable: true },
        { key: 'collected_at', label: 'Last Seen', sortable: true, render: function(v) { return timeAgo(v); } },
      ], 'investigate-arp', res.arp_hits);
    }

    // MAC table
    if (res.mac_hits && res.mac_hits.length) {
      hasResults = true;
      document.getElementById('section-mac').style.display = '';
      document.getElementById('mac-count').textContent = '(' + res.mac_hits.length + ')';
      makeTable('mac-table-wrap', [
        { key: 'node_name', label: 'Node', sortable: true, render: function(v) { return nodeLink(v); } },
        { key: 'mac', label: 'MAC', sortable: true, render: function(v) { return macLink(v); } },
        { key: 'interface', label: 'Interface', sortable: true },
        { key: 'vlan', label: 'VLAN', sortable: true, render: function(v) { return (v && v !== 0 && v !== '0') ? esc(String(v)) : '-'; } },
        { key: 'entry_type', label: 'Type', sortable: true },
        { key: 'collected_at', label: 'Last Seen', sortable: true, render: function(v) { return timeAgo(v); } },
      ], 'investigate-mac', res.mac_hits);
    }

    // Routes
    if (res.routes && res.routes.length) {
      hasResults = true;
      document.getElementById('section-routes').style.display = '';
      document.getElementById('route-count').textContent = '(' + res.routes.length + ')';
      makeTable('route-table-wrap', [
        { key: 'node_name', label: 'Node', sortable: true, render: function(v) { return nodeLink(v); } },
        { key: 'prefix', label: 'Prefix', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
        { key: 'next_hop', label: 'Next Hop', sortable: true, render: function(v) { return esc(v || 'connected'); } },
        { key: 'protocol', label: 'Protocol', sortable: true },
        { key: 'metric', label: 'Metric', sortable: true, render: function(v) { return v != null ? v : '-'; } },
        { key: 'outgoing_interface', label: 'Interface', sortable: true },
        { key: 'vrf', label: 'VRF', sortable: true },
      ], 'investigate-routes', res.routes);
    }

    // FIB
    if (res.fib && res.fib.length) {
      hasResults = true;
      document.getElementById('section-fib').style.display = '';
      document.getElementById('fib-count').textContent = '(' + res.fib.length + ')';
      makeTable('fib-table-wrap', [
        { key: 'node_name', label: 'Node', sortable: true, render: function(v) { return nodeLink(v); } },
        { key: 'prefix', label: 'Prefix', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
        { key: 'next_hop', label: 'Next Hop', sortable: true },
        { key: 'interface', label: 'Interface', sortable: true },
        { key: 'vrf', label: 'VRF', sortable: true },
        { key: 'source', label: 'Source', sortable: true },
      ], 'investigate-fib', res.fib);
    }

    // Gateways
    if (res.gateways && res.gateways.length) {
      hasResults = true;
      document.getElementById('section-gateways').style.display = '';
      document.getElementById('gw-count').textContent = '(' + res.gateways.length + ')';
      makeTable('gw-table-wrap', [
        { key: 'node_name', label: 'Node', sortable: true, render: function(v) { return nodeLink(v); } },
        { key: 'prefix', label: 'Prefix', sortable: true, render: function(v) { return '<code>' + esc(v) + '</code>'; } },
        { key: 'next_hop', label: 'Next Hop', sortable: true },
        { key: 'protocol', label: 'Protocol', sortable: true },
        { key: 'outgoing_interface', label: 'Interface', sortable: true },
      ], 'investigate-gateways', res.gateways);
    }

    // LLDP
    if (res.lldp && res.lldp.length) {
      hasResults = true;
      document.getElementById('section-lldp').style.display = '';
      document.getElementById('lldp-count').textContent = '(' + res.lldp.length + ')';
      makeTable('lldp-table-wrap', [
        { key: 'node_name', label: 'Node', sortable: true, render: function(v) { return nodeLink(v); } },
        { key: 'local_interface', label: 'Local Interface', sortable: true },
        { key: 'remote_system_name', label: 'Remote System', sortable: true },
        { key: 'remote_port', label: 'Remote Port', sortable: true },
        { key: 'remote_management_ip', label: 'Mgmt IP', sortable: true },
      ], 'investigate-lldp', res.lldp);
    }

    if (!hasResults) {
      document.getElementById('no-results').style.display = '';
    }
  } catch (e) {
    info.innerHTML = 'Search error: ' + esc(e.message);
  }
}

// ---- Event handlers ----
document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') doSearch();
});

// Load query from URL params
(function() {
  var params = new URLSearchParams(window.location.search);
  var q = params.get('q');
  if (q) document.getElementById('search-input').value = q;
  if (params.get('node')) document.getElementById('filter-node').value = params.get('node');
  if (params.get('vlan')) document.getElementById('filter-vlan').value = params.get('vlan');
  if (params.get('type')) document.getElementById('filter-type').value = params.get('type');
  if (q) doSearch();
})();

// ---- Auth / theme boilerplate ----
document.getElementById('logout-link').addEventListener('click', async function(e) {
  e.preventDefault(); await fetch('/api/auth/logout', { method: 'POST' }); window.location.href = '/login';
});
function setTheme(t) { document.documentElement.setAttribute('data-theme', t); localStorage.setItem('mnm-theme', t); document.querySelectorAll('.theme-switcher button').forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-theme') === t); }); }
(function() { var s = localStorage.getItem('mnm-theme') || 'dark'; setTheme(s); document.querySelectorAll('.theme-switcher button').forEach(function(b) { b.addEventListener('click', function() { setTheme(b.getAttribute('data-theme')); }); }); })();
MNMPreferences.init();
(async function() { var r = await fetch('/api/auth/check'); var d = await r.json(); if (!d.authenticated) window.location.href = '/login'; })();
