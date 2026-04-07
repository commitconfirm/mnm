/**
 * MNM DataTable — reusable paginated, sortable table component.
 *
 * Renders a table with sortable column headers, pagination controls,
 * and a "Showing X-Y of Z results" indicator. Preferences (page size,
 * sort column/direction) persist in localStorage per storageKey.
 *
 * Usage:
 *   const dt = new DataTable({
 *     containerId: 'my-table-container',
 *     columns: [
 *       { key: 'ip', label: 'IP', sortable: true },
 *       { key: 'mac', label: 'MAC', sortable: true, render: (val, row) => `<code>${val}</code>` },
 *     ],
 *     pageSize: 100,
 *     pageSizes: [25, 50, 100, 500, 1000],
 *     storageKey: 'mnm-endpoints-table',
 *   });
 *
 *   dt.setData(arrayOfObjects);
 *   dt.render();
 */

class DataTable {
  constructor(options) {
    this.containerId = options.containerId;
    this.columns = options.columns;
    this.storageKey = options.storageKey || 'mnm-datatable';
    this.pageSizes = options.pageSizes || [25, 50, 100, 500, 1000];

    // Load preferences from localStorage
    const prefs = this._loadPrefs();
    this.pageSize = prefs.pageSize || options.pageSize || 100;
    this.currentPage = 1;
    this.sortCol = prefs.sortCol || null;
    this.sortDir = prefs.sortDir || 'asc';
    this.visibleColumns = prefs.visibleColumns || this.columns.map(c => c.key);

    this.data = [];
    this.filteredData = [];
    this.filterFn = null;
  }

  /**
   * Replace the full dataset. Resets to page 1 if current page exceeds
   * the new total page count.
   */
  setData(data) {
    this.data = data || [];
    this._applyFilter();
    this._applySort();
    const totalPages = Math.max(1, Math.ceil(this.filteredData.length / this.pageSize));
    if (this.currentPage > totalPages) {
      this.currentPage = 1;
    }
  }

  /**
   * Set an external filter function. fn(row) => boolean.
   * Call render() after setting the filter.
   */
  setFilter(fn) {
    this.filterFn = fn;
    this._applyFilter();
    this._applySort();
    const totalPages = Math.max(1, Math.ceil(this.filteredData.length / this.pageSize));
    if (this.currentPage > totalPages) {
      this.currentPage = 1;
    }
  }

  _applyFilter() {
    if (this.filterFn) {
      this.filteredData = this.data.filter(this.filterFn);
    } else {
      this.filteredData = this.data.slice();
    }
  }

  _applySort() {
    if (!this.sortCol) return;
    const col = this.sortCol;
    const dir = this.sortDir;
    this.filteredData.sort((a, b) => {
      let va = a[col];
      let vb = b[col];

      // Handle nulls — push to end
      if (va == null && vb == null) return 0;
      if (va == null) return 1;
      if (vb == null) return -1;

      // Numeric comparison if both sides are numbers
      const na = Number(va);
      const nb = Number(vb);
      if (!isNaN(na) && !isNaN(nb) && typeof va !== 'boolean' && typeof vb !== 'boolean') {
        const cmp = na - nb;
        return dir === 'asc' ? cmp : -cmp;
      }

      // Date detection (ISO strings)
      if (typeof va === 'string' && typeof vb === 'string') {
        const da = Date.parse(va);
        const db = Date.parse(vb);
        if (!isNaN(da) && !isNaN(db) && va.includes('-') && vb.includes('-')) {
          const cmp = da - db;
          return dir === 'asc' ? cmp : -cmp;
        }
      }

      // String comparison
      va = String(va).toLowerCase();
      vb = String(vb).toLowerCase();
      let cmp = 0;
      if (va < vb) cmp = -1;
      else if (va > vb) cmp = 1;
      return dir === 'asc' ? cmp : -cmp;
    });
  }

  _getPage() {
    const start = (this.currentPage - 1) * this.pageSize;
    const end = start + this.pageSize;
    return this.filteredData.slice(start, end);
  }

  _getTotalPages() {
    return Math.max(1, Math.ceil(this.filteredData.length / this.pageSize));
  }

  render() {
    const container = document.getElementById(this.containerId);
    if (!container) return;

    const pageRows = this._getPage();
    const total = this.filteredData.length;
    const totalPages = this._getTotalPages();
    const start = total > 0 ? (this.currentPage - 1) * this.pageSize + 1 : 0;
    const end = Math.min(this.currentPage * this.pageSize, total);

    // Build pagination controls
    const paginationHtml = '<div class="datatable-controls">'
      + '<div class="datatable-info">Showing ' + start + '-' + end + ' of ' + total + ' results</div>'
      + '<div class="datatable-pagination">'
      + '<button data-dt-action="first"' + (this.currentPage <= 1 ? ' disabled' : '') + '>First</button>'
      + '<button data-dt-action="prev"' + (this.currentPage <= 1 ? ' disabled' : '') + '>Prev</button>'
      + '<span class="page-info">Page ' + this.currentPage + ' of ' + totalPages + '</span>'
      + '<button data-dt-action="next"' + (this.currentPage >= totalPages ? ' disabled' : '') + '>Next</button>'
      + '<button data-dt-action="last"' + (this.currentPage >= totalPages ? ' disabled' : '') + '>Last</button>'
      + '<select data-dt-action="pagesize">' + this.pageSizes.map(s => {
          return '<option value="' + s + '"' + (s === this.pageSize ? ' selected' : '') + '>' + s + ' / page</option>';
        }).join('') + '</select>'
      + '</div></div>';

    // Build visible columns
    const visCols = this.columns.filter(c => this.visibleColumns.includes(c.key));

    // Build table header
    const theadCells = visCols.map(c => {
      if (c.sortable) {
        let cls = 'sortable';
        if (this.sortCol === c.key) cls += ' ' + this.sortDir;
        return '<th class="' + cls + '" data-dt-sort="' + c.key + '">' + c.label + '</th>';
      }
      return '<th>' + c.label + '</th>';
    }).join('');

    // Build table body
    const tbodyRows = pageRows.map(row => {
      const cells = visCols.map(c => {
        const val = row[c.key];
        const rendered = c.render ? c.render(val, row) : this._escHtml(val != null ? String(val) : '-');
        return '<td>' + rendered + '</td>';
      }).join('');
      return '<tr>' + cells + '</tr>';
    }).join('');

    const emptyRow = total === 0
      ? '<tr><td colspan="' + visCols.length + '" style="text-align:center;color:var(--text-muted);padding:24px;">No results</td></tr>'
      : '';

    container.innerHTML = paginationHtml
      + '<table><thead><tr>' + theadCells + '</tr></thead>'
      + '<tbody>' + (tbodyRows || emptyRow) + '</tbody></table>'
      + (total > this.pageSize ? paginationHtml : '');

    // Attach event listeners
    this._attachEvents(container);
  }

  _attachEvents(container) {
    const self = this;

    // Sort headers
    container.querySelectorAll('th[data-dt-sort]').forEach(th => {
      th.addEventListener('click', () => {
        const col = th.getAttribute('data-dt-sort');
        if (self.sortCol === col) {
          if (self.sortDir === 'asc') {
            self.sortDir = 'desc';
          } else {
            // Third click clears sort
            self.sortCol = null;
            self.sortDir = 'asc';
          }
        } else {
          self.sortCol = col;
          self.sortDir = 'asc';
        }
        self._applySort();
        self._savePrefs();
        self.render();
      });
    });

    // Pagination buttons
    container.querySelectorAll('button[data-dt-action]').forEach(btn => {
      btn.addEventListener('click', () => {
        const action = btn.getAttribute('data-dt-action');
        const totalPages = self._getTotalPages();
        if (action === 'first') self.currentPage = 1;
        else if (action === 'prev') self.currentPage = Math.max(1, self.currentPage - 1);
        else if (action === 'next') self.currentPage = Math.min(totalPages, self.currentPage + 1);
        else if (action === 'last') self.currentPage = totalPages;
        self.render();
      });
    });

    // Page size selector
    container.querySelectorAll('select[data-dt-action="pagesize"]').forEach(sel => {
      sel.addEventListener('change', () => {
        self.pageSize = parseInt(sel.value, 10);
        self.currentPage = 1;
        self._savePrefs();
        self.render();
      });
    });
  }

  _escHtml(str) {
    if (!str) return '';
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  _loadPrefs() {
    try {
      return JSON.parse(localStorage.getItem(this.storageKey) || '{}');
    } catch (e) {
      return {};
    }
  }

  _savePrefs() {
    localStorage.setItem(this.storageKey, JSON.stringify({
      pageSize: this.pageSize,
      sortCol: this.sortCol,
      sortDir: this.sortDir,
      visibleColumns: this.visibleColumns,
    }));
  }
}
