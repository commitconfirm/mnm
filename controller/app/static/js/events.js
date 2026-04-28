/* MNM Events — network activity feed */

function escHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

async function checkAuth() {
  const r = await fetch('/api/auth/check');
  const d = await r.json();
  if (!d.authenticated) window.location.href = '/login';
}

async function loadEvents() {
  const type = document.getElementById('filter-type').value;
  const since = document.getElementById('filter-since').value;
  const params = new URLSearchParams({ since, limit: '500' });
  if (type) params.set('type', type);

  const resp = await fetch('/api/endpoints/events?' + params.toString());
  if (!resp.ok) return;
  const data = await resp.json();
  const tbody = document.getElementById('events-table');
  if (!data.events.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-muted)">No events in this window</td></tr>';
    return;
  }
  tbody.innerHTML = data.events.map(e => {
    const watched = e.details && e.details.watched === true;
    const watchBadge = watched ? ' <span class="badge warning" title="Watched MAC">👁</span>' : '';
    const rowStyle = watched ? ' style="background:rgba(255,200,0,0.08)"' : '';
    const macLink = '<a href="/endpoints/' + encodeURIComponent(e.mac_address) + '"><code>' + escHtml(e.mac_address) + '</code></a>' + watchBadge;
    return '<tr' + rowStyle + '>' +
      '<td>' + escHtml(new Date(e.timestamp).toLocaleString()) + '</td>' +
      '<td>' + macLink + '</td>' +
      '<td><span class="badge">' + escHtml(e.event_type) + '</span></td>' +
      '<td>' + escHtml(e.old_value || '-') + '</td>' +
      '<td>' + escHtml(e.new_value || '-') + '</td>' +
      '<td><code style="font-size:0.75rem">' + escHtml(JSON.stringify(e.details || {})) + '</code></td>' +
    '</tr>';
  }).join('');
}

function renderEndpointRow(m) {
  return '<li><a href="/endpoints/' + encodeURIComponent(m.mac_address) + '"><code>' +
    escHtml(m.mac_address) + '</code></a> &mdash; ' + escHtml(m.mac_vendor || '') +
    ' (' + escHtml(m.current_switch || '?') + '/' + escHtml(m.current_port || '?') +
    (m.current_ip ? ' &mdash; ' + escHtml(m.current_ip) : '') +
    (m.hostname ? ' &mdash; ' + escHtml(m.hostname) : '') + ')</li>';
}

async function loadAnomalies() {
  const resp = await fetch('/api/endpoints/anomalies');
  if (!resp.ok) return;
  const a = await resp.json();
  const summary = a.summary || {};
  const sumEl = document.getElementById('anomalies-summary');
  sumEl.innerHTML =
    '<div class="grid-4" style="margin-bottom:12px">' +
    '<div class="stat"><div class="value">' + (summary.ip_conflicts || 0) + '</div><div class="label">IP Conflicts</div></div>' +
    '<div class="stat"><div class="value">' + (summary.multi_location || 0) + '</div><div class="label">Multi-Location MACs</div></div>' +
    '<div class="stat"><div class="value">' + (summary.no_ip || 0) + '</div><div class="label">No IP</div></div>' +
    '<div class="stat"><div class="value">' + (summary.unclassified || 0) + '</div><div class="label">Unclassified</div></div>' +
    '</div>';

  const det = document.getElementById('anomalies-detail');
  let html = '';

  if (a.ip_conflicts && a.ip_conflicts.length) {
    html += '<h3>IP Conflicts</h3>';
    a.ip_conflicts.forEach(c => {
      html += '<div class="alert warning"><strong>' + escHtml(c.ip) + '</strong> claimed by ' +
        c.mac_count + ' MACs:<ul>' + c.macs.map(renderEndpointRow).join('') + '</ul></div>';
    });
  }

  if (a.multi_location && a.multi_location.length) {
    html += '<h3>MACs on Multiple Switches</h3>';
    a.multi_location.forEach(c => {
      html += '<div class="alert warning"><code>' + escHtml(c.mac) + '</code> active on ' +
        c.location_count + ' switches:<ul>' + c.locations.map(renderEndpointRow).join('') + '</ul></div>';
    });
  }

  if (a.no_ip && a.no_ip.length) {
    html += '<h3>Endpoints With No IP (' + a.no_ip.length + ')</h3>' +
      '<table><thead><tr><th>MAC</th><th>Vendor</th><th>Hostname</th><th>Switch</th><th>Port</th><th>VLAN</th></tr></thead><tbody>' +
      a.no_ip.map(m =>
        '<tr><td><a href="/endpoints/' + encodeURIComponent(m.mac_address) + '"><code>' + escHtml(m.mac_address) + '</code></a></td>' +
        '<td>' + escHtml(m.mac_vendor || '') + '</td>' +
        '<td>' + escHtml(m.hostname || '') + '</td>' +
        '<td>' + escHtml(m.current_switch || '') + '</td>' +
        '<td>' + escHtml(m.current_port || '') + '</td>' +
        '<td>' + (m.vlan || '') + '</td></tr>'
      ).join('') + '</tbody></table>';
  }

  if (a.unclassified && a.unclassified.length) {
    html += '<h3>Unclassified Endpoints (' + a.unclassified.length + ')</h3>' +
      '<table><thead><tr><th>MAC</th><th>Vendor</th><th>IP</th><th>Switch</th><th>Port</th></tr></thead><tbody>' +
      a.unclassified.map(m =>
        '<tr><td><a href="/endpoints/' + encodeURIComponent(m.mac_address) + '"><code>' + escHtml(m.mac_address) + '</code></a></td>' +
        '<td>' + escHtml(m.mac_vendor || '') + '</td>' +
        '<td>' + escHtml(m.current_ip || '') + '</td>' +
        '<td>' + escHtml(m.current_switch || '') + '</td>' +
        '<td>' + escHtml(m.current_port || '') + '</td></tr>'
      ).join('') + '</tbody></table>';
  }

  if (a.stale && a.stale.length) {
    html += '<h3>Stale Endpoints — not seen in 7+ days (' + a.stale.length + ')</h3>' +
      '<table><thead><tr><th>MAC</th><th>Hostname</th><th>Last Seen</th><th>Switch/Port</th></tr></thead><tbody>' +
      a.stale.map(m =>
        '<tr><td><a href="/endpoints/' + encodeURIComponent(m.mac_address) + '"><code>' + escHtml(m.mac_address) + '</code></a></td>' +
        '<td>' + escHtml(m.hostname || '') + '</td>' +
        '<td>' + escHtml(m.last_seen || '') + '</td>' +
        '<td>' + escHtml((m.current_switch || '') + '/' + (m.current_port || '')) + '</td></tr>'
      ).join('') + '</tbody></table>';
  }

  if (!html) html = '<p style="color:var(--text-muted)">No anomalies detected.</p>';
  det.innerHTML = html;
}

async function loadConflicts() { return loadAnomalies(); }

document.getElementById('filter-type').addEventListener('change', loadEvents);
document.getElementById('filter-since').addEventListener('change', loadEvents);
document.getElementById('refresh-btn').addEventListener('click', () => { loadEvents(); loadConflicts(); });
document.getElementById('logout-link').addEventListener('click', async (e) => {
  e.preventDefault();
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
});

function setTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  localStorage.setItem('mnm-theme', t);
  document.querySelectorAll('.theme-switcher button').forEach(b => {
    b.classList.toggle('active', b.getAttribute('data-theme') === t);
  });
}
(function () {
  setTheme(localStorage.getItem('mnm-theme') || 'dark');
  document.querySelectorAll('.theme-switcher button').forEach(b => {
    b.addEventListener('click', () => setTheme(b.getAttribute('data-theme')));
  });
})();

// Column-help tooltip on the Event column (static <th>; see
// docs/UI_CONVENTIONS.md). Injected once at page load.
(function injectColHelp() {
  if (typeof MNMColHelp === 'undefined') return;
  const el = document.querySelector('[data-col-help="event-type"]');
  if (!el) return;
  el.innerHTML = MNMColHelp.icon({
    title: 'What changed for this MAC',
    values: [
      ['appeared',         'First sighting of this MAC anywhere on the network.'],
      ['moved_port',       'MAC moved to a different port on the same switch.'],
      ['moved_switch',     'MAC appeared on a different switch than before.'],
      ['ip_changed',       'MAC kept its location but its observed IP changed.'],
      ['hostname_changed', 'MAC kept its location but its observed hostname changed.'],
    ],
  });
})();

checkAuth().then(() => { loadEvents(); loadConflicts(); });
