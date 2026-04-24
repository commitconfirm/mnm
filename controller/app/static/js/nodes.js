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

// v1.0 Prompt 8: render Nautobot status + Phase 2 state as a badge
// with inline Retry Phase 2 button when stuck. Colors match the
// design conventions: green=Active, yellow=Onboarding Incomplete,
// red=Onboarding Failed, gray=other (e.g. Staged, Inventory). phase2
// running/pending show an hourglass; failed surfaces the retry button.
function statusBadge(node) {
  var statusName = node.status_name || '';
  var p2 = node.phase2_state;  // null | "pending" | "running" | "completed" | "failed"
  var color, label, extra = '';
  if (statusName === 'Active') { color = 'green'; label = 'Active'; }
  else if (statusName === 'Onboarding Incomplete') { color = 'yellow'; label = 'Incomplete'; }
  else if (statusName === 'Onboarding Failed') { color = 'red'; label = 'Failed'; }
  else { color = 'grey'; label = statusName || '-'; }

  if (p2 === 'running') {
    extra = ' <span title="Phase 2 running">&#8987;</span>';
  } else if (p2 === 'failed' || (statusName === 'Onboarding Incomplete' && p2 !== 'running')) {
    // Include the retry button inline with the badge. Cache-miss race
    // (Prompt 7.5 observation): first polling-loop dispatch after
    // Phase 1 may show "failed" fleetingly; operator retry drives to
    // success. Prompt 9 hardens at the polling layer.
    extra = ' <button class="btn small retry-phase2-btn" data-node="' +
      escHtml(node.name) + '" title="Re-enable phase2_populate; polling loop picks it up within ~30s">Retry Phase 2</button>';
  }
  return '<span class="badge ' + color + '">' + escHtml(label) + '</span>' + extra;
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
    { key: 'status_name', label: 'Status', sortable: true, render: function(_v, row) { return statusBadge(row); } },
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
  attachRetryPhase2Buttons();
}

function attachRetryPhase2Buttons() {
  document.querySelectorAll('.retry-phase2-btn').forEach(function(btn) {
    btn.addEventListener('click', async function() {
      var nodeName = btn.getAttribute('data-node');
      btn.disabled = true;
      btn.textContent = 'Retrying...';
      try {
        var r = await fetch('/api/onboarding/retry-phase2/' + encodeURIComponent(nodeName), { method: 'POST' });
        if (!r.ok) {
          var err = await r.json().catch(function() { return {}; });
          btn.textContent = 'Failed';
          alert('Retry failed: ' + (err.detail || r.statusText));
          return;
        }
        // Polling loop picks up the re-enabled row within
        // ~POLL_CHECK_INTERVAL (default 30s). Refresh the nodes list
        // so the operator sees the transition once it completes.
        btn.textContent = 'Queued...';
        setTimeout(function() { loadNodes(); }, 5000);
      } catch (e) {
        btn.textContent = 'Error';
        alert('Retry error: ' + e.message);
      }
    });
  });
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

// ---------------------------------------------------------------------------
// v1.0 Prompt 8: Add Node modal — single-device manual onboarding via
// the direct-REST orchestrator. Same backend endpoint as the Discover
// page's retry-onboard button; surfaces Phase 1 result synchronously
// and the new device appears in the Nodes list after Phase 2 polls
// complete (next polling-loop tick, ≤30s).
// ---------------------------------------------------------------------------

async function populateAddNodeDropdowns() {
  try {
    const [locResp, sgResp] = await Promise.all([
      fetch('/api/nautobot/locations'),
      fetch('/api/nautobot/secrets-groups'),
    ]);
    if (locResp.ok) {
      const data = await locResp.json();
      const sel = document.getElementById('add-location');
      sel.innerHTML = '<option value="">-- select --</option>';
      (data.locations || []).forEach(function(loc) {
        var opt = document.createElement('option');
        opt.value = loc.id;
        opt.textContent = loc.display || loc.name || loc.id;
        sel.appendChild(opt);
      });
    }
    if (sgResp.ok) {
      const data = await sgResp.json();
      const sel = document.getElementById('add-secrets-group');
      sel.innerHTML = '<option value="">-- select --</option>';
      (data.secrets_groups || []).forEach(function(sg) {
        var opt = document.createElement('option');
        opt.value = sg.id;
        opt.textContent = sg.display || sg.name || sg.id;
        sel.appendChild(opt);
      });
    }
  } catch (e) {
    console.error('Dropdown populate failed:', e);
  }
}

function openAddNodeModal() {
  document.getElementById('add-ip').value = '';
  document.getElementById('add-community').value = '';
  document.getElementById('add-node-status').textContent = '';
  document.getElementById('add-node-submit').disabled = false;
  document.getElementById('add-node-modal').style.display = 'flex';
  populateAddNodeDropdowns();
}

function closeAddNodeModal() {
  document.getElementById('add-node-modal').style.display = 'none';
}

async function submitAddNode() {
  var ip = document.getElementById('add-ip').value.trim();
  var community = document.getElementById('add-community').value;
  var locId = document.getElementById('add-location').value;
  var sgId = document.getElementById('add-secrets-group').value;
  var statusEl = document.getElementById('add-node-status');
  var submitBtn = document.getElementById('add-node-submit');
  // Minimal IPv4 validation — backend does the authoritative check.
  if (!/^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$/.test(ip)) {
    statusEl.textContent = 'Enter a valid IPv4 address.';
    statusEl.style.color = 'var(--danger, #b71c1c)';
    return;
  }
  if (!community || !locId || !sgId) {
    statusEl.textContent = 'All fields required.';
    statusEl.style.color = 'var(--danger, #b71c1c)';
    return;
  }
  submitBtn.disabled = true;
  statusEl.style.color = 'var(--muted)';
  statusEl.textContent = 'Phase 1: classifying + creating device...';
  try {
    var r = await fetch('/api/onboarding/direct-rest', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        ip: ip, snmp_community: community,
        secrets_group_id: sgId, location_id: locId,
      }),
    });
    var d = await r.json();
    if (!r.ok || !d.success) {
      if (d && d.error_type === 'AlreadyOnboardedError') {
        statusEl.style.color = 'var(--warning, #d08800)';
        statusEl.textContent = 'Already onboarded as ' + (d.device_name || '(unknown)') +
          '. View it in the list below.';
      } else {
        statusEl.style.color = 'var(--danger, #b71c1c)';
        statusEl.textContent = 'Failed: ' + (d.error || r.statusText || 'unknown error');
      }
      submitBtn.disabled = false;
      return;
    }
    // Phase 1 complete; Phase 2 runs via polling loop within ~30s.
    statusEl.style.color = 'var(--muted)';
    statusEl.textContent = 'Phase 1 complete: ' + d.device_name +
      '. Phase 2 (populate interfaces) runs in the background; refreshing in 5s...';
    setTimeout(function() { loadNodes(); closeAddNodeModal(); }, 5000);
  } catch (e) {
    statusEl.style.color = 'var(--danger, #b71c1c)';
    statusEl.textContent = 'Request failed: ' + e.message;
    submitBtn.disabled = false;
  }
}

document.getElementById('add-node-btn').addEventListener('click', openAddNodeModal);
document.getElementById('add-node-cancel').addEventListener('click', closeAddNodeModal);
document.getElementById('add-node-submit').addEventListener('click', submitAddNode);
document.getElementById('add-node-modal').addEventListener('click', function(e) {
  if (e.target && e.target.id === 'add-node-modal') closeAddNodeModal();
});


checkAuth().then(function() {
  loadNodes();
  startAutoRefresh();
});
