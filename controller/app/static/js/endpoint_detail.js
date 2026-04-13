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
    ['Switch', (ep.current_switch && ep.current_switch !== '(none)') ? ep.current_switch : ''],
    ['Port', (ep.current_port && ep.current_port !== '(none)') ? ep.current_port : ''],
    ['VLAN', (ep.current_vlan && ep.current_vlan !== 0) ? ep.current_vlan : ''],
    ['Classification', ep.classification, true],
    ['Confidence', ep.classification_confidence || ''],
    ['First Seen', ep.first_seen],
    ['Last Seen', ep.last_seen],
    ['Source', ep.source],
  ];
  meta.innerHTML = fields.map(f => {
    var val = f[2] ? MNMIcons.deviceIcon(f[1] || 'unknown') + ' ' + escHtml(f[1] || '-') : escHtml(f[1] || '-');
    return '<div class="stat"><div class="value" style="font-size:1rem">' + val +
    '</div><div class="label">' + escHtml(f[0]) + '</div></div>';
  }).join('');

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

  // Probe + comments + change history
  await loadProbeResult(mac);
  await loadComments(mac);
  await loadChangeHistory(mac);
}

// ---- Probe ----
async function loadProbeResult(mac) {
  try {
    var r = await fetch('/api/probes/results?mac=' + encodeURIComponent(mac));
    if (!r.ok) return;
    var d = await r.json();
    var el = document.getElementById('probe-result');
    if (!d || !d.probed_at) {
      el.innerHTML = '<span style="color:var(--text-muted)">No probe data. Click "Probe Now" to test reachability.</span>';
      return;
    }
    var when = new Date(d.probed_at).toLocaleString();
    if (d.reachable) {
      el.innerHTML = '<span class="dot green" style="width:8px;height:8px"></span> '
        + '<strong>Reachable</strong> &mdash; '
        + (d.latency_ms != null ? d.latency_ms + ' ms' : '')
        + ' via ' + escHtml(d.probe_type)
        + (d.tcp_port ? ':' + d.tcp_port : '')
        + (d.packet_loss != null ? ', ' + Math.round(d.packet_loss * 100) + '% loss' : '')
        + ' <span style="color:var(--text-muted);font-size:0.8rem"> &mdash; ' + escHtml(when) + '</span>';
    } else {
      el.innerHTML = '<span class="dot red" style="width:8px;height:8px"></span> '
        + '<strong>Unreachable</strong>'
        + ' <span style="color:var(--text-muted);font-size:0.8rem"> &mdash; ' + escHtml(when) + '</span>';
    }
  } catch (e) { /* ignore */ }
}

document.getElementById('probe-now-btn').addEventListener('click', async function() {
  var btn = this;
  var mac = getMac();
  btn.disabled = true; btn.textContent = 'Probing...';
  document.getElementById('probe-result').innerHTML = '<span style="color:var(--text-muted)">Probing...</span>';
  try {
    await fetch('/api/probes/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ macs: [mac] }),
    });
    // Poll for completion
    var attempts = 0;
    var poll = setInterval(async function() {
      attempts++;
      var state = await (await fetch('/api/probes/status')).json();
      if (!state.running || attempts > 15) {
        clearInterval(poll);
        await loadProbeResult(mac);
        btn.disabled = false; btn.textContent = 'Probe Now';
      }
    }, 1000);
  } catch (e) {
    btn.disabled = false; btn.textContent = 'Probe Now';
    document.getElementById('probe-result').innerHTML = '<span style="color:var(--red)">Probe failed: ' + escHtml(e.message) + '</span>';
  }
});

// ---- Comments ----
async function loadComments(mac) {
  try {
    const r = await fetch('/api/comments?target_type=endpoint&target_id=' + encodeURIComponent(mac));
    if (!r.ok) return;
    const comments = await r.json();
    document.getElementById('comment-count').textContent = comments.length ? '(' + comments.length + ')' : '';
    const list = document.getElementById('comment-list');
    if (!comments.length) {
      list.innerHTML = '<div style="color:var(--text-muted);font-size:0.85rem;padding:8px 0">No comments yet.</div>';
      return;
    }
    list.innerHTML = comments.map(function(c) {
      var when = new Date(c.created_at).toLocaleString();
      return '<div style="padding:10px 12px;background:var(--bg-elevated);border:1px solid var(--border);border-radius:var(--radius);margin-bottom:8px">'
        + '<div style="display:flex;justify-content:space-between;font-size:0.75rem;color:var(--text-muted);margin-bottom:4px">'
        + '<span><strong>' + escHtml(c.created_by) + '</strong> &middot; ' + escHtml(when) + '</span>'
        + '<button class="btn small" style="padding:2px 8px;font-size:0.72rem" onclick="deleteComment(\'' + escHtml(c.id) + '\', \'' + escHtml(mac) + '\')">Delete</button>'
        + '</div>'
        + '<div style="white-space:pre-wrap">' + escHtml(c.comment_text) + '</div>'
        + '</div>';
    }).join('');
  } catch (e) { console.error('Failed to load comments:', e); }
}

async function submitComment(mac) {
  const input = document.getElementById('comment-input');
  const text = input.value.trim();
  if (!text) return;
  const btn = document.getElementById('comment-submit');
  btn.disabled = true;
  try {
    const r = await fetch('/api/comments', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ target_type: 'endpoint', target_id: mac, comment_text: text }),
    });
    if (!r.ok) { alert('Failed to add comment'); return; }
    input.value = '';
    await loadComments(mac);
    await loadChangeHistory(mac);
  } catch (e) { alert('Failed: ' + e.message); }
  btn.disabled = false;
}

async function deleteComment(commentId, mac) {
  if (!confirm('Delete this comment?')) return;
  try {
    const r = await fetch('/api/comments/' + encodeURIComponent(commentId), { method: 'DELETE' });
    if (r.status !== 204) { alert('Failed to delete'); return; }
    await loadComments(mac);
    await loadChangeHistory(mac);
  } catch (e) { alert('Failed: ' + e.message); }
}

// ---- Change History ----
var historyDT = null;

async function loadChangeHistory(mac) {
  try {
    const r = await fetch('/api/history?target_type=endpoint&target_id=' + encodeURIComponent(mac) + '&limit=200');
    if (!r.ok) return;
    const history = await r.json();
    document.getElementById('history-count').textContent = history.length ? '(' + history.length + ')' : '(none)';
    if (!historyDT) {
      historyDT = new DataTable({
        containerId: 'history-dt',
        columns: [
          { key: 'changed_at', label: 'When', sortable: true, render: function(v) { return '<span title="' + escHtml(v) + '">' + new Date(v).toLocaleString() + '</span>'; } },
          { key: 'field_name', label: 'Field', sortable: true, render: function(v) { return '<code>' + escHtml(v) + '</code>'; } },
          { key: 'old_value', label: 'Old', sortable: false, render: function(v) { return v == null ? '<span style="color:var(--text-muted)">—</span>' : escHtml(v); } },
          { key: 'new_value', label: 'New', sortable: false, render: function(v) { return v == null ? '<span style="color:var(--text-muted)">—</span>' : escHtml(v); } },
          { key: 'change_source', label: 'Source', sortable: true, render: function(v) { return '<span class="badge">' + escHtml(v) + '</span>'; } },
        ],
        pageSize: 25,
        storageKey: 'mnm-endpoint-history',
      });
    }
    historyDT.setData(history);
    historyDT.render();
  } catch (e) { console.error('Failed to load change history:', e); }
}

// Wire up comment submit + history toggle after DOM is ready
document.getElementById('comment-submit').addEventListener('click', function() {
  submitComment(getMac());
});
document.getElementById('history-toggle').addEventListener('click', function() {
  const container = document.getElementById('history-container');
  const btn = this;
  if (container.style.display === 'none') {
    container.style.display = '';
    btn.innerHTML = btn.innerHTML.replace('&#9658;', '&#9660;');
  } else {
    container.style.display = 'none';
    btn.innerHTML = btn.innerHTML.replace('&#9660;', '&#9658;');
  }
});

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
