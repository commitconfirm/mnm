/**
 * MNM client-side table export (CSV + JSON).
 *
 * Pure DOM extraction: what the operator sees is what gets exported.
 * No server round-trip. If pagination is active and the user is on
 * page 3 of N, only page 3 is exported — documented UX. Applying
 * filters before export is correct behavior (operator sees X, gets X).
 *
 * Integration pattern (see docs/UI_CONVENTIONS.md):
 *   <button onclick="MNMTableExport.exportCSV(document.querySelector('#my-table-dt table'), 'mnm-foo')">Export CSV</button>
 *   <button onclick="MNMTableExport.exportJSON(document.querySelector('#my-table-dt table'), 'mnm-foo')">Export JSON</button>
 *
 * Filenames auto-suffix with the current ISO date (YYYY-MM-DD).
 *
 * Pure helpers (window.MNMTableExport.rowsToCSV / rowsToJSON) are
 * exposed for unit testing via Node.
 */
(function() {
  function extractFromTable(tableEl) {
    if (!tableEl) return { headers: [], rows: [] };
    // Strip sort-indicator glyphs/asc-desc/arrow chars embedded by
    // DataTable into <th> text so the CSV header is just the label.
    const headers = Array.from(tableEl.querySelectorAll('thead th')).map(function(th) {
      // Prefer a data-export-label attribute if set (lets pages
      // override for columns where visible header includes an icon).
      const explicit = th.getAttribute('data-export-label');
      if (explicit != null) return explicit;
      return (th.textContent || '').replace(/[↑↓ⓘⓙ⓪Ⓞℹⓑ]/g, '').trim();
    });
    const rows = Array.from(tableEl.querySelectorAll('tbody tr')).map(function(tr) {
      return Array.from(tr.querySelectorAll('td')).map(function(td) {
        // Prefer data-export attribute so icon-only cells export
        // the semantic value, not an empty string or raw icon.
        const explicit = td.getAttribute('data-export');
        if (explicit != null) return explicit;
        return (td.textContent || '').trim();
      });
    });
    // Drop entirely-empty row (the DataTable's "No results" row has
    // a single td with colspan=N — collapses to one element we want
    // to filter out).
    const nonEmpty = rows.filter(function(r) {
      return r.length > 1 || (r.length === 1 && r[0] !== '' && r[0].toLowerCase() !== 'no results');
    });
    return { headers: headers, rows: nonEmpty };
  }

  function rowsToCSV(headers, rows) {
    // RFC 4180: fields containing ", \n, or \r must be quoted;
    // embedded " doubled.
    function esc(v) {
      const s = v == null ? '' : String(v);
      if (/["\n\r,]/.test(s)) return '"' + s.replace(/"/g, '""') + '"';
      return s;
    }
    const lines = [headers.map(esc).join(',')];
    rows.forEach(function(r) {
      lines.push(r.map(esc).join(','));
    });
    return lines.join('\r\n');
  }

  function rowsToJSON(headers, rows) {
    return rows.map(function(r) {
      const obj = {};
      headers.forEach(function(h, i) {
        obj[h] = (r[i] != null) ? r[i] : '';
      });
      return obj;
    });
  }

  function isoDate() {
    // UTC day — stable across operator timezones.
    const d = new Date();
    const y = d.getUTCFullYear();
    const m = String(d.getUTCMonth() + 1).padStart(2, '0');
    const day = String(d.getUTCDate()).padStart(2, '0');
    return y + '-' + m + '-' + day;
  }

  function download(content, filename, mimetype) {
    const blob = new Blob([content], { type: mimetype + ';charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function() { URL.revokeObjectURL(url); }, 0);
  }

  function exportCSV(tableEl, baseName) {
    const parts = extractFromTable(tableEl);
    const csv = rowsToCSV(parts.headers, parts.rows);
    download(csv, baseName + '-' + isoDate() + '.csv', 'text/csv');
  }

  function exportJSON(tableEl, baseName) {
    const parts = extractFromTable(tableEl);
    const json = JSON.stringify(rowsToJSON(parts.headers, parts.rows), null, 2);
    download(json, baseName + '-' + isoDate() + '.json', 'application/json');
  }

  /**
   * Create and return the two export buttons wired to a table element
   * resolved lazily (so it works with DataTables that re-render). The
   * selector is resolved at click time, not at button-build time.
   */
  function makeButtons(tableSelector, baseName) {
    const wrap = document.createElement('div');
    wrap.className = 'table-export-buttons';
    const btnCSV = document.createElement('button');
    btnCSV.className = 'btn small';
    btnCSV.title = 'Export as CSV (current page, current filters)';
    btnCSV.textContent = 'CSV';
    btnCSV.addEventListener('click', function() {
      const t = document.querySelector(tableSelector);
      if (t) exportCSV(t, baseName);
    });
    const btnJSON = document.createElement('button');
    btnJSON.className = 'btn small';
    btnJSON.title = 'Export as JSON (current page, current filters)';
    btnJSON.textContent = 'JSON';
    btnJSON.addEventListener('click', function() {
      const t = document.querySelector(tableSelector);
      if (t) exportJSON(t, baseName);
    });
    wrap.appendChild(btnCSV);
    wrap.appendChild(btnJSON);
    return wrap;
  }

  const api = {
    exportCSV: exportCSV,
    exportJSON: exportJSON,
    extractFromTable: extractFromTable,
    rowsToCSV: rowsToCSV,
    rowsToJSON: rowsToJSON,
    isoDate: isoDate,
    makeButtons: makeButtons,
  };

  if (typeof window !== 'undefined') window.MNMTableExport = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})();
