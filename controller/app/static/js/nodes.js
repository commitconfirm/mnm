/* MNM Nodes — onboarded infrastructure device list */

let allNodes = [];
let refreshTimer = null;

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

function healthDot(health, label) {
  const colorMap = { green: 'green', yellow: 'yellow', red: 'red', gray: 'grey' };
  const dotClass = colorMap[health] || 'grey';
  return '<span class="dot-wrap">' +
    '<span class="dot ' + dotClass + '"></span>' +
    '<span class="dot-tooltip">' + escHtml(label) + '</span>' +
    '</span>';
}

function jobDots(jobs) {
  if (!jobs || Object.keys(jobs).length === 0) return '-';
  const types = ['arp', 'mac', 'dhcp', 'lldp', 'routes', 'bgp'];
  return types.map(function(jt) {
    const j = jobs[jt];
    if (!j) return '';
    let color = 'grey';
    let tip = jt.toUpperCase() + ': no data';
    if (j.last_error && !j.last_success) {
      color = 'red';
      tip = jt.toUpperCase() + ': ' + (j.last_error || 'error').substring(0, 80);
    } else if (j.last_success) {
      color = 'green';
      tip = jt.toUpperCase() + ': OK (' + timeAgo(j.last_success) + ')';
      if (j.last_error) {
        color = 'yellow';
        tip = jt.toUpperCase() + ': recovered, last error: ' + (j.last_error || '').substring(0, 60);
      }
    }
    if (!j.enabled) {
      color = 'grey';
      tip = jt.toUpperCase() + ': disabled';
    }
    return '<span class="dot-wrap" style="margin-right:4px">' +
      '<span class="dot ' + color + '" style="width:8px;height:8px"></span>' +
      '<span class="dot-tooltip">' + escHtml(tip) + '</span>' +
      '</span>';
  }).join('');
}

// DataTable instance
const nodesDT = new DataTable({
  containerId: 'nodes-dt',
  columns: [
    { key: 'health', label: 'Health', sortable: true, render: function(v, row) {
      return healthDot(v, row.health_label || '');
    } },
    { key: 'name', label: 'Name', sortable: true, render: function(v) {
      return '<a href="/nodes/' + encodeURIComponent(v) + '"><strong>' + escHtml(v) + '</strong></a>';
    } },
    { key: 'platform', label: 'Platform', sortable: true, render: function(v) { return escHtml(v || '-'); } },
    { key: 'primary_ip', label: 'Primary IP', sortable: true, render: function(v) { if (!v) return '-'; return '<code>' + escHtml(v.replace(/\/\d+$/, '')) + '</code>'; } },
    { key: 'role', label: 'Role', sortable: true, render: function(v) { return escHtml(v || '-'); } },
    { key: 'location', label: 'Location', sortable: true, render: function(v) { return escHtml(v || '-'); } },
    { key: 'jobs', label: 'Poll Jobs', sortable: false, render: function(v) { return jobDots(v); } },
    { key: 'last_polled', label: 'Last Polled', sortable: true, render: function(v) {
      return '<span title="' + escHtml(v || '') + '">' + timeAgo(v) + '</span>';
    } },
    { key: 'name', label: '', sortable: false, render: function(v) {
      return '<button class="btn small poll-now-btn" data-node="' + escHtml(v) + '">Poll Now</button>';
    } },
  ],
  pageSize: 100,
  storageKey: 'mnm-nodes-table',
});

async function loadNodes() {
  try {
    const resp = await fetch('/api/nodes');
    if (resp.status === 401) { window.location.href = '/login'; return; }
    const data = await resp.json();
    allNodes = data.nodes || [];
    updateSummary();
    populateFilters();
    applyFiltersAndRender();
    attachPollButtons();
  } catch (e) {
    console.error('Failed to load nodes:', e);
  }
}

function updateSummary() {
  document.getElementById('total-nodes').textContent = allNodes.length;
  document.getElementById('healthy-nodes').textContent = allNodes.filter(function(n) { return n.health === 'green'; }).length;
  document.getElementById('failing-nodes').textContent = allNodes.filter(function(n) { return n.health === 'red'; }).length;
  const platforms = new Set(allNodes.map(function(n) { return n.platform; }).filter(Boolean));
  document.getElementById('platforms-count').textContent = platforms.size;
}

function populateFilters() {
  populateDropdown('filter-platform', uniqueValues('platform'), 'All Platforms');
  populateDropdown('filter-location', uniqueValues('location'), 'All Locations');
}

function uniqueValues(field) {
  const vals = new Set();
  allNodes.forEach(function(n) {
    if (n[field]) vals.add(String(n[field]));
  });
  return Array.from(vals).sort();
}

function populateDropdown(id, values, defaultLabel) {
  const sel = document.getElementById(id);
  const current = sel.value;
  sel.innerHTML = '<option value="">' + defaultLabel + '</option>';
  values.forEach(function(v) {
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
  const healthVal = document.getElementById('filter-health').value;
  const platformVal = document.getElementById('filter-platform').value;
  const locationVal = document.getElementById('filter-location').value;

  return function(node) {
    if (search) {
      const name = (node.name || '').toLowerCase();
      const ip = (node.primary_ip || '').toLowerCase();
      const plat = (node.platform || '').toLowerCase();
      if (!name.includes(search) && !ip.includes(search) && !plat.includes(search)) {
        return false;
      }
    }
    if (healthVal && node.health !== healthVal) return false;
    if (platformVal && node.platform !== platformVal) return false;
    if (locationVal && node.location !== locationVal) return false;
    return true;
  };
}

function applyFiltersAndRender() {
  nodesDT.setData(allNodes);
  nodesDT.setFilter(buildFilterFn());
  nodesDT.render();
  attachPollButtons();
}

function attachPollButtons() {
  document.querySelectorAll('.poll-now-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      const nodeName = btn.getAttribute('data-node');
      btn.disabled = true;
      btn.textContent = 'Polling...';
      try {
        const resp = await fetch('/api/polling/trigger/' + encodeURIComponent(nodeName), { method: 'POST' });
        if (resp.ok) {
          btn.textContent = 'Triggered';
          setTimeout(function() { loadNodes(); }, 3000);
        } else {
          btn.textContent = 'Failed';
          setTimeout(function() { btn.textContent = 'Poll Now'; btn.disabled = false; }, 2000);
        }
      } catch (e) {
        btn.textContent = 'Error';
        setTimeout(function() { btn.textContent = 'Poll Now'; btn.disabled = false; }, 2000);
      }
    });
  });
}

// Filter handlers
document.getElementById('filter-search').addEventListener('input', applyFiltersAndRender);
document.getElementById('filter-health').addEventListener('change', applyFiltersAndRender);
document.getElementById('filter-platform').addEventListener('change', applyFiltersAndRender);
document.getElementById('filter-location').addEventListener('change', applyFiltersAndRender);

// Logout
document.getElementById('logout-link').addEventListener('click', async function(e) {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
});

// Theme switcher
function setTheme(theme) {
  document.documentElement.setAttribute('data-theme', theme);
  localStorage.setItem('mnm-theme', theme);
  document.querySelectorAll('.theme-switcher button').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-theme') === theme);
  });
}

function initTheme() {
  var saved = localStorage.getItem('mnm-theme') || 'dark';
  setTheme(saved);
  document.querySelectorAll('.theme-switcher button').forEach(function(btn) {
    btn.addEventListener('click', function() { setTheme(btn.getAttribute('data-theme')); });
  });
}

// Init
initTheme();
MNMPreferences.init();

function startAutoRefresh() {
  if (refreshTimer) { clearInterval(refreshTimer); refreshTimer = null; }
  var interval = MNMPreferences.get('autoRefreshInterval');
  if (interval && interval > 0) {
    refreshTimer = setInterval(loadNodes, interval * 1000);
  }
}

window.addEventListener('mnm-preferences-changed', function() {
  startAutoRefresh();
});

checkAuth().then(function() {
  loadNodes();
  startAutoRefresh();
});
