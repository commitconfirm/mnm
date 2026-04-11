/* MNM Global Network Search */

function esc(s) { if (!s) return ''; var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }
function timeAgo(iso) {
  if (!iso) return '-';
  var d = Date.now() - new Date(iso).getTime();
  if (d < 0) return 'just now';
  var m = Math.floor(d / 60000);
  if (m < 1) return 'just now'; if (m < 60) return m + 'm ago';
  var h = Math.floor(m / 60);
  if (h < 24) return h + 'h ago'; return Math.floor(h / 24) + 'd ago';
}

async function doSearch() {
  var q = document.getElementById('search-input').value.trim();
  if (!q) return;

  // Update URL
  history.replaceState(null, '', '/search?q=' + encodeURIComponent(q));

  document.getElementById('search-results').style.display = 'block';
  document.getElementById('result-type').textContent = 'Searching...';

  // Hide all sections
  ['arp', 'mac', 'routes', 'lldp', 'endpoints'].forEach(function(s) {
    document.getElementById('section-' + s).style.display = 'none';
  });
  document.getElementById('no-results').style.display = 'none';

  try {
    var r = await fetch('/api/search?q=' + encodeURIComponent(q));
    if (!r.ok) { document.getElementById('result-type').textContent = 'Search failed'; return; }
    var d = await r.json();
    var res = d.results || {};
    var hasResults = false;

    document.getElementById('result-type').textContent =
      'Query: "' + esc(d.query) + '" — detected as ' + d.type;

    // ARP results
    var arp = res.arp_table || [];
    if (arp.length) {
      hasResults = true;
      document.getElementById('section-arp').style.display = '';
      document.getElementById('arp-count').textContent = '(' + arp.length + ')';
      document.getElementById('arp-results').innerHTML = arp.map(function(a) {
        return '<tr><td><a href="/nodes/' + encodeURIComponent(a.node_name) + '">' + esc(a.node_name) + '</a></td>'
          + '<td>' + esc(a.ip) + '</td><td><code>' + esc(a.mac) + '</code></td>'
          + '<td>' + esc(a.interface) + '</td><td>' + timeAgo(a.collected_at) + '</td></tr>';
      }).join('');
    }

    // MAC results
    var mac = res.mac_table || [];
    if (mac.length) {
      hasResults = true;
      document.getElementById('section-mac').style.display = '';
      document.getElementById('mac-count').textContent = '(' + mac.length + ')';
      document.getElementById('mac-results').innerHTML = mac.map(function(m) {
        return '<tr><td><a href="/nodes/' + encodeURIComponent(m.node_name) + '">' + esc(m.node_name) + '</a></td>'
          + '<td><code>' + esc(m.mac) + '</code></td><td>' + esc(m.interface) + '</td>'
          + '<td>' + (m.vlan || '-') + '</td><td>' + esc(m.entry_type) + '</td>'
          + '<td>' + timeAgo(m.collected_at) + '</td></tr>';
      }).join('');
    }

    // Route results
    var routes = res.routes || [];
    if (routes.length) {
      hasResults = true;
      document.getElementById('section-routes').style.display = '';
      document.getElementById('route-count').textContent = '(' + routes.length + ')';
      document.getElementById('route-results').innerHTML = routes.map(function(rt) {
        return '<tr><td><a href="/nodes/' + encodeURIComponent(rt.node_name) + '">' + esc(rt.node_name) + '</a></td>'
          + '<td><code>' + esc(rt.prefix) + '</code></td><td>' + esc(rt.next_hop || 'connected') + '</td>'
          + '<td>' + esc(rt.protocol) + '</td><td>' + (rt.metric != null ? rt.metric : '-') + '</td>'
          + '<td>' + esc(rt.vrf) + '</td></tr>';
      }).join('');
    }

    // LLDP results
    var lldp = res.lldp || [];
    if (lldp.length) {
      hasResults = true;
      document.getElementById('section-lldp').style.display = '';
      document.getElementById('lldp-count').textContent = '(' + lldp.length + ')';
      document.getElementById('lldp-results').innerHTML = lldp.map(function(l) {
        return '<tr><td><a href="/nodes/' + encodeURIComponent(l.node_name) + '">' + esc(l.node_name) + '</a></td>'
          + '<td>' + esc(l.local_interface) + '</td>'
          + '<td>' + esc(l.remote_system_name) + '</td><td>' + esc(l.remote_port) + '</td></tr>';
      }).join('');
    }

    // Endpoint results
    var eps = res.endpoints || [];
    var ep = res.endpoint;
    if (ep && !eps.length) eps = [ep];
    if (eps.length) {
      hasResults = true;
      document.getElementById('section-endpoints').style.display = '';
      document.getElementById('ep-count').textContent = '(' + eps.length + ')';
      document.getElementById('ep-results').innerHTML = eps.map(function(e) {
        return '<tr><td><a href="/endpoints/' + encodeURIComponent(e.mac || e.mac_address) + '"><code>' + esc(e.mac || e.mac_address) + '</code></a></td>'
          + '<td>' + esc(e.ip || e.current_ip) + '</td><td>' + esc(e.hostname) + '</td>'
          + '<td>' + esc(e.device_name || e.current_switch) + '</td>'
          + '<td>' + esc(e.switch_port || e.current_port) + '</td>'
          + '<td>' + (e.vlan || e.current_vlan || '-') + '</td></tr>';
      }).join('');
    }

    if (!hasResults) {
      document.getElementById('no-results').style.display = '';
    }
  } catch (e) {
    document.getElementById('result-type').textContent = 'Search error: ' + e.message;
  }
}

// Event handlers
document.getElementById('search-btn').addEventListener('click', doSearch);
document.getElementById('search-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') doSearch();
});

// Load query from URL
var params = new URLSearchParams(window.location.search);
var initialQ = params.get('q');
if (initialQ) {
  document.getElementById('search-input').value = initialQ;
  doSearch();
}

// Logout + theme
document.getElementById('logout-link').addEventListener('click', async function(e) {
  e.preventDefault(); await fetch('/api/auth/logout', { method: 'POST' }); window.location.href = '/login';
});
function setTheme(t) { document.documentElement.setAttribute('data-theme', t); localStorage.setItem('mnm-theme', t); document.querySelectorAll('.theme-switcher button').forEach(function(b) { b.classList.toggle('active', b.getAttribute('data-theme') === t); }); }
(function() { var s = localStorage.getItem('mnm-theme') || 'dark'; setTheme(s); document.querySelectorAll('.theme-switcher button').forEach(function(b) { b.addEventListener('click', function() { setTheme(b.getAttribute('data-theme')); }); }); })();
MNMPreferences.init();

// Auth
(async function() { var r = await fetch('/api/auth/check'); var d = await r.json(); if (!d.authenticated) window.location.href = '/login'; })();
