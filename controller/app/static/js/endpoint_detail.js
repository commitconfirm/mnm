/* MNM Endpoint Detail — per-MAC timeline view */

function escHtml(s) {
  if (s == null) return '';
  const d = document.createElement('div');
  d.textContent = String(s);
  return d.innerHTML;
}

function getMac() {
  const parts = window.location.pathname.split('/');
  return decodeURIComponent(parts[parts.length - 1] || '');
}

async function checkAuth() {
  const r = await fetch('/api/auth/check');
  const d = await r.json();
  if (!d.authenticated) window.location.href = '/login';
}

async function load() {
  const mac = getMac();
  document.getElementById('ep-title').textContent = 'Endpoint ' + mac;

  const tlResp = await fetch('/api/endpoints/' + encodeURIComponent(mac) + '/timeline');
  if (!tlResp.ok) {
    document.getElementById('ep-timeline').innerHTML = '<p>Endpoint not found.</p>';
    return;
  }
  const tl = await tlResp.json();
  const ep = tl.endpoint || {};

  // Meta
  const meta = document.getElementById('ep-meta');
  const allIps = (ep.all_ips && ep.all_ips.length) ? ep.all_ips.join(', ') : (ep.current_ip || '-');
  const fields = [
    ['MAC', ep.mac_address || mac],
    ['Vendor', ep.mac_vendor],
    ['Current IP', ep.current_ip],
    ['All Known IPs', allIps],
    ['Hostname', ep.hostname],
    ['Switch', ep.current_switch],
    ['Port', ep.current_port],
    ['VLAN', ep.current_vlan],
    ['Classification', ep.classification],
    ['First Seen', ep.first_seen],
    ['Last Seen', ep.last_seen],
    ['Source', ep.source],
  ];
  meta.innerHTML = fields.map(f =>
    '<div class="stat"><div class="value" style="font-size:1rem">' + escHtml(f[1] || '-') +
    '</div><div class="label">' + escHtml(f[0]) + '</div></div>'
  ).join('');

  // Timeline (oldest first narrative)
  const tlEl = document.getElementById('ep-timeline');
  if (!tl.timeline.length) {
    tlEl.innerHTML = '<p>No events recorded yet.</p>';
  } else {
    tlEl.innerHTML = '<ul class="timeline">' + tl.timeline.map(e =>
      '<li><strong>' + escHtml(new Date(e.timestamp).toLocaleString()) + '</strong> &mdash; ' +
      escHtml(e.text) + ' <span class="badge">' + escHtml(e.event_type) + '</span></li>'
    ).join('') + '</ul>';
  }

  // Raw events table
  const histResp = await fetch('/api/endpoints/' + encodeURIComponent(mac) + '/history');
  const hist = await histResp.json();
  const tbody = document.getElementById('ep-events');
  if (!hist.events.length) {
    tbody.innerHTML = '<tr><td colspan="4" style="text-align:center;color:var(--text-muted)">No events</td></tr>';
  } else {
    tbody.innerHTML = hist.events.map(e =>
      '<tr><td>' + escHtml(new Date(e.timestamp).toLocaleString()) + '</td>' +
      '<td>' + escHtml(e.event_type) + '</td>' +
      '<td>' + escHtml(e.old_value || '-') + '</td>' +
      '<td>' + escHtml(e.new_value || '-') + '</td></tr>'
    ).join('');
  }
}

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
(function initTheme() {
  setTheme(localStorage.getItem('mnm-theme') || 'dark');
  document.querySelectorAll('.theme-switcher button').forEach(b => {
    b.addEventListener('click', () => setTheme(b.getAttribute('data-theme')));
  });
})();

checkAuth().then(load);
