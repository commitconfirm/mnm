/**
 * MNM column-header help tooltip helper.
 *
 * Use when a table column's values are a non-obvious enum (e.g. the
 * Endpoints "Source" column: "Sweep" / "Infrastructure" / "Both").
 * The helper returns an HTML snippet to inline into a DataTable
 * column ``label`` so the header renders "Source ⓘ" with a hover
 * tooltip explaining each value.
 *
 * Convention (see docs/UI_CONVENTIONS.md): apply ONLY to columns
 * whose values are enums the operator has to interpret; do not
 * wrap every column.
 *
 * Usage:
 *   { key: 'source', label: 'Source' + MNMColHelp.icon({
 *       values: [
 *         ['Sweep', 'Discovered via sweep probes …'],
 *         ['Infrastructure', 'Discovered via ARP/MAC/LLDP …'],
 *         ['Both', 'Correlated across both sources.'],
 *       ],
 *       docsLink: '/docs/ENDPOINTS.md#source-column',
 *   }) }
 */
(function() {
  function escHtml(s) {
    const d = document.createElement('div');
    d.textContent = String(s || '');
    return d.innerHTML;
  }

  function buildTooltipHtml(opts) {
    const title = opts.title ? '<div class="col-help-title">' + escHtml(opts.title) + '</div>' : '';
    const items = (opts.values || []).map(function(pair) {
      return '<div class="col-help-item"><span class="col-help-key">'
        + escHtml(pair[0]) + '</span>'
        + '<span class="col-help-desc">' + escHtml(pair[1]) + '</span></div>';
    }).join('');
    const link = opts.docsLink
      ? '<div class="col-help-link"><a href="' + escHtml(opts.docsLink)
        + '" target="_blank" rel="noopener">Docs</a></div>'
      : '';
    return title + items + link;
  }

  function icon(opts) {
    const tipHtml = buildTooltipHtml(opts);
    return ' <span class="col-help-wrap" tabindex="0" aria-label="Column help">'
      + '<span class="col-help-icon">&#9432;</span>'
      + '<span class="col-help-tip">' + tipHtml + '</span>'
      + '</span>';
  }

  window.MNMColHelp = { icon: icon, _buildTooltipHtml: buildTooltipHtml };
})();
