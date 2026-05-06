/* MNM Endpoints — endpoint table page logic */

let allEndpoints = [];
let refreshTimer = null;
let excludedIpSet = new Set();
let nodeMacSet = new Set();
let nodeIpSet = new Set();

async function loadExcludedIps() {
  try {
    const r = await fetch('/api/discover/excludes');
    if (!r.ok) return;
    const data = await r.json();
    excludedIpSet = new Set((data.excludes || [])
      .filter(e => e.type === 'ip')
      .map(e => e.identifier));
  } catch (e) { /* non-fatal — table renders without strikethrough */ }
}

async function loadNodeIdentifiers() {
  try {
    const r = await fetch('/api/nodes/macs');
    if (!r.ok) return;
    const data = await r.json();
    nodeMacSet = new Set((data.macs || []).map(m => m.toUpperCase()));
    nodeIpSet = new Set(data.ips || []);
  } catch (e) { /* non-fatal — endpoints render unfiltered */ }
}

async function checkAuth() {
  const resp = await fetch('/api/auth/check');
  const data = await resp.json();
  if (!data.authenticated) {
    window.location.href = '/login';
  }
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
  if (diff < 0) return 'just now';
  const mins = Math.floor(diff / 60000);
  if (mins < 1) return 'just now';
  if (mins < 60) return mins + 'm ago';
  const hours = Math.floor(mins / 60);
  if (hours < 24) return hours + 'h ago';
  const days = Math.floor(hours / 24);
  return days + 'd ago';
}

function sourceBadge(source) {
  if (!source) return '-';
  const cls = source.toLowerCase();
  return '<span class="badge ' + cls + '">' + escHtml(source) + '</span>';
}

// DataTable instance for endpoints
const endpointsDT = new DataTable({
  containerId: 'endpoints-dt',
  columns: [
    { key: 'classification', label: '', sortable: true, render: function(v) { return MNMIcons.deviceIcon(v || 'unknown', 'sm'); } },
    { key: 'ip', label: 'IP', sortable: true, render: function(v) {
      if (!v) return '-';
      if (excludedIpSet.has(v)) {
        return '<span title="Excluded from discovery" style="text-decoration: line-through; opacity: 0.6">' + escHtml(v) + '</span>';
      }
      return escHtml(v);
    } },
    { key: 'mac', label: 'MAC', sortable: true, render: function(v) { return '<a href="/endpoints/' + encodeURIComponent(v) + '"><code>' + escHtml(v) + '</code></a>'; } },
    { key: 'mac_vendor', label: 'Vendor', sortable: true, render: function(v) { return escHtml(v || '-'); } },
    { key: 'hostname', label: 'Hostname', sortable: true, render: function(v, row) { return escHtml(v || row.dhcp_hostname || '-'); } },
    { key: 'device_name', label: 'Switch', sortable: true, render: function(v, row) { var s = v || row.switch || ''; return (s && s !== '(none)') ? escHtml(s) : '-'; } },
    { key: 'switch_port', label: 'Port', sortable: true, render: function(v, row) { var p = v || row.port || ''; return (p && p !== '(none)') ? escHtml(p) : '-'; } },
    { key: 'vlan', label: 'VLAN', sortable: true, render: function(v) { return (v != null && v !== 0 && v !== '0') ? escHtml(String(v)) : '-'; } },
    { key: 'first_seen', label: 'First Seen', sortable: true, render: function(v) { return '<span title="' + escHtml(v || '') + '">' + timeAgo(v) + '</span>'; } },
    { key: 'last_seen', label: 'Last Seen', sortable: true, render: function(v) { return '<span title="' + escHtml(v || '') + '">' + timeAgo(v) + '</span>'; } },
    {
      key: 'source',
      label: 'Source' + MNMColHelp.icon({
        title: 'Where this endpoint record came from',
        values: [
          ['Infrastructure', 'Discovered via ARP, MAC, or LLDP data collected from onboarded nodes (switches, routers, firewalls).'],
          ['Sweep',          'Discovered directly via sweep probes (SNMP, port scans, banners) against the endpoint IP.'],
          ['Both',           'Correlated across both sources.'],
        ],
        docsLink: '/docs/ENDPOINTS.md',
      }),
      exportLabel: 'Source',
      sortable: true,
      render: function(v) { return sourceBadge(v); },
    },
  ],
  pageSize: 100,
  storageKey: 'mnm-endpoints-table',
});

async function loadSummary() {
  try {
    const resp = await fetch('/api/endpoints/summary');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    document.getElementById('total-endpoints').textContent = data.total_endpoints != null ? data.total_endpoints : '-';
    document.getElementById('vlans-active').textContent = data.vlans_active != null ? data.vlans_active : '-';
    document.getElementById('vendors-seen').textContent = data.vendors_seen != null ? data.vendors_seen : '-';
    document.getElementById('last-collection').textContent = data.last_collection ? timeAgo(data.last_collection) : '-';
  } catch (e) {
    console.error('Failed to load endpoint summary:', e);
  }
}

async function loadEndpoints() {
  try {
    const resp = await fetch('/api/endpoints');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    // Filter out onboarded nodes — endpoints page shows passively-discovered only
    // Match by MAC (if Nautobot has interface MACs) OR by IP (primary_ip4)
    const raw = data.endpoints || [];
    allEndpoints = (nodeMacSet.size > 0 || nodeIpSet.size > 0)
      ? raw.filter(ep => !nodeMacSet.has((ep.mac || '').toUpperCase()) && !nodeIpSet.has(ep.ip || ''))
      : raw;
    populateFilters();
    applyFiltersAndRender();
  } catch (e) {
    console.error('Failed to load endpoints:', e);
  }
}

function populateFilters() {
  populateDropdown('filter-switch', uniqueValues('device_name'), 'All Switches');
  populateDropdown('filter-vlan', uniqueValues('vlan'), 'All VLANs');
  populateDropdown('filter-vendor', uniqueValues('mac_vendor'), 'All Vendors');
}

function uniqueValues(field) {
  const vals = new Set();
  allEndpoints.forEach(ep => {
    var v = ep[field];
    if (!v) return;
    // Skip sentinel values
    if (field === 'vlan' && (v === 0 || v === '0')) return;
    if (String(v) === '(none)') return;
    vals.add(String(v));
  });
  return Array.from(vals).sort();
}

function populateDropdown(id, values, defaultLabel) {
  const sel = document.getElementById(id);
  const current = sel.value;
  sel.innerHTML = '<option value="">' + defaultLabel + '</option>';
  values.forEach(v => {
    const opt = document.createElement('option');
    opt.value = v;
    opt.textContent = v;
    sel.appendChild(opt);
  });
  if (current && values.includes(current)) {
    sel.value = current;
  }
}

function buildFilterFn() {
  const search = document.getElementById('filter-search').value.toLowerCase();
  const switchVal = document.getElementById('filter-switch').value;
  const vlanVal = document.getElementById('filter-vlan').value;
  const vendorVal = document.getElementById('filter-vendor').value;
  const sourceVal = document.getElementById('filter-source').value;

  return function(ep) {
    if (search) {
      const ip = (ep.ip || '').toLowerCase();
      const mac = (ep.mac || '').toLowerCase();
      const hostname = (ep.hostname || '').toLowerCase();
      if (!ip.includes(search) && !mac.includes(search) && !hostname.includes(search)) {
        return false;
      }
    }
    if (switchVal && String((ep.device_name || ep.switch)) !== switchVal) return false;
    if (vlanVal && String(ep.vlan) !== vlanVal) return false;
    if (vendorVal && String(ep.mac_vendor || '') !== vendorVal) return false;
    if (sourceVal && (ep.source || '').toLowerCase() !== sourceVal) return false;
    return true;
  };
}

function applyFiltersAndRender() {
  endpointsDT.setData(allEndpoints);
  endpointsDT.setFilter(buildFilterFn());
  endpointsDT.render();
}

// Filter handlers
document.getElementById('filter-search').addEventListener('input', applyFiltersAndRender);
document.getElementById('filter-switch').addEventListener('change', applyFiltersAndRender);
document.getElementById('filter-vlan').addEventListener('change', applyFiltersAndRender);
document.getElementById('filter-vendor').addEventListener('change', applyFiltersAndRender);
document.getElementById('filter-source').addEventListener('change', applyFiltersAndRender);

// Trigger collection with progress polling
let collectionPoll = null;
let collectionPollObserved = false;

async function pollCollection() {
  try {
    const resp = await fetch('/api/endpoints/collection-status');
    if (!resp.ok) return;
    const data = await resp.json();
    const btn = document.getElementById('trigger-collection-btn');

    if (data.running) {
      collectionPollObserved = true;
      const p = data.progress || {};
      btn.textContent = `Collecting... ${p.devices_done || 0}/${p.devices_total || '?'} devices`;
    } else if (!collectionPollObserved) {
      // Race: POST returned before collect_all() flipped running=true.
      // Keep polling rather than declaring done.
      return;
    } else {
      collectionPollObserved = false;
      btn.textContent = 'Trigger Collection';
      btn.disabled = false;
      if (collectionPoll) { clearInterval(collectionPoll); collectionPoll = null; }
      loadEndpoints();
      loadSummary();
      loadHistory();
    }
  } catch (e) { /* ignore */ }
}

async function loadHistory() {
  try {
    const resp = await fetch('/api/endpoints/history');
    if (!resp.ok) return;
    const data = await resp.json();
    const history = data.history || [];
    const tbody = document.getElementById('collection-history-table');

    if (history.length === 0) {
      tbody.innerHTML = '<tr><td colspan="6" style="color: var(--text-muted); text-align: center;">No collection history yet</td></tr>';
      return;
    }

    tbody.innerHTML = history.map(h => {
      const started = h.started_at ? new Date(h.started_at).toLocaleString() : '-';
      const dur = h.duration_seconds != null ? h.duration_seconds + 's' : '-';
      return `<tr>
        <td>${started}</td>
        <td>${dur}</td>
        <td>${h.devices_queried || 0}${h.devices_failed ? ' (' + h.devices_failed + ' failed)' : ''}</td>
        <td>${h.endpoints_found || 0}</td>
        <td>${h.endpoints_recorded || 0}</td>
        <td>${h.record_failed || 0}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    console.error('Failed to load collection history:', e);
  }
}

document.getElementById('trigger-collection-btn').addEventListener('click', async () => {
  const btn = document.getElementById('trigger-collection-btn');
  btn.disabled = true;
  btn.textContent = 'Collecting...';
  collectionPollObserved = false;
  try {
    const resp = await fetch('/api/endpoints/collect', { method: 'POST' });
    if (resp.status === 401) { window.location.href = '/login'; return; }
    if (resp.status === 409) {
      btn.textContent = 'Collection in progress...';
    }
    // Poll progress regardless
    if (!collectionPoll) {
      collectionPoll = setInterval(pollCollection, 2000);
    }
  } catch (e) {
    console.error('Failed to trigger collection:', e);
    btn.textContent = 'Trigger Collection';
    btn.disabled = false;
  }
});

// Logout
document.getElementById('logout-link').addEventListener('click', async (e) => {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
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

function startAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  const interval = MNMPreferences.get('autoRefreshInterval');
  if (interval && interval > 0) {
    refreshTimer = setInterval(() => {
      loadSummary();
      loadEndpoints();
    }, interval * 1000);
  }
}

window.addEventListener('mnm-preferences-changed', () => {
  startAutoRefresh();
});

// ---------------------------------------------------------------------------
// Watchlist
// ---------------------------------------------------------------------------
async function loadWatches() {
  try {
    const r = await fetch('/api/endpoints/watches');
    if (!r.ok) return;
    const data = await r.json();
    const tbody = document.getElementById('watches-tbody');
    if (!data.watches || !data.watches.length) {
      tbody.innerHTML = '<tr><td colspan="5" style="color:var(--text-muted); text-align:center">No watches</td></tr>';
      return;
    }
    tbody.innerHTML = data.watches.map(w =>
      '<tr><td><code>' + escHtml(w.mac_address) + '</code></td>' +
      '<td>' + escHtml(w.reason) + '</td>' +
      '<td>' + escHtml(w.created_by) + '</td>' +
      '<td>' + escHtml(w.created_at ? new Date(w.created_at).toLocaleString() : '') + '</td>' +
      '<td><button class="btn watch-remove" data-mac="' + escHtml(w.mac_address) + '">Remove</button></td></tr>'
    ).join('');
    document.querySelectorAll('.watch-remove').forEach(btn => {
      btn.addEventListener('click', async () => {
        const mac = btn.getAttribute('data-mac');
        await fetch('/api/endpoints/watches/' + encodeURIComponent(mac), { method: 'DELETE' });
        loadWatches();
      });
    });
  } catch (e) { console.error('Failed to load watches:', e); }
}

document.getElementById('watch-add-btn').addEventListener('click', async () => {
  const mac = document.getElementById('watch-mac').value.trim();
  const reason = document.getElementById('watch-reason').value.trim();
  if (!mac) return;
  const r = await fetch('/api/endpoints/watches', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ mac_address: mac, reason }),
  });
  if (r.ok) {
    document.getElementById('watch-mac').value = '';
    document.getElementById('watch-reason').value = '';
    loadWatches();
  } else {
    alert('Failed to add watch: ' + r.status);
  }
});

checkAuth().then(async () => {
  await loadExcludedIps();  // populate set before first table render
  await loadNodeIdentifiers();  // filter out infrastructure nodes by MAC and IP
  loadSummary();
  loadEndpoints();
  loadHistory();
  loadWatches();
  startAutoRefresh();
  const exportHost = document.getElementById('endpoints-export-buttons');
  if (exportHost) {
    exportHost.replaceWith(
      MNMTableExport.makeButtons('#endpoints-dt table', 'mnm-endpoints')
    );
  }
});
