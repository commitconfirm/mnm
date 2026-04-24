# UI Conventions (MNM Controller Operational Pages)

Short operator-UX guide for the controller's operational pages
(Discovery, Nodes, Endpoints, Investigations, Jobs, Logs). The goal
is consistency across pages so operators don't have to re-learn
patterns per screen. Introduced in v1.0 Prompt 9 as the pattern
page count grew past ad-hoc.

## 1. Column-header help tooltips

Apply a help icon + tooltip to any table column whose values are a
**non-obvious enum** the operator has to interpret. Don't wrap every
column — only the ones where the value is short jargon or a status
label that isn't self-explanatory from the text alone.

**When to apply**

- Column value is an enum: `Sweep` / `Infrastructure` / `Both`,
  `Active` / `Incomplete` / `Failed`, `Green` / `Yellow` / `Red`.
- Column value is a short code that the operator is expected to
  decode: a protocol name from a small set, a short classification.

**When NOT to apply**

- Column is plain text the operator reads at face value (name,
  hostname, IP, platform string).
- Column value is a timestamp or count.
- Column is self-documenting via adjacent columns.

**How to apply**

Use the shared helper:

```js
{
  key: 'source',
  label: 'Source' + MNMColHelp.icon({
    title: 'Where this endpoint record came from',
    values: [
      ['Infrastructure', 'Discovered via ARP/MAC/LLDP from onboarded nodes.'],
      ['Sweep',          'Discovered via direct sweep probes.'],
      ['Both',           'Correlated across both sources.'],
    ],
    docsLink: '/docs/ENDPOINTS.md',
  }),
  exportLabel: 'Source',  // keep CSV header plain (no icon glyph)
  sortable: true,
  render: (v) => sourceBadge(v),
}
```

Include the source file in the page's HTML:
`<script src="/static/js/col-help.js"></script>`

Tooltip structure: optional title, then one `[value, description]`
pair per line, optional "Docs" link at the bottom.

## 2. Column alignment

- **Icon-only cells** (status dots, health dots, small glyphs):
  centered.
- **Numeric cells** (counts, durations): centered.
- **Text cells** (name, hostname, platform, location): left-aligned
  (default).
- **Action cells** (buttons): centered.
- **Column headers**: match the alignment of their data cells.

**In DataTable** columns, opt in with `align: 'center'`:

```js
{ key: 'health', label: '…', align: 'center', render: ... }
```

**In static tables** (like `discover.html`'s sweep-results table),
apply `class="align-center"` to `<th>` and per-row `<td>` elements.

The CSS lives in `static/css/style.css` under the "Column-alignment
conventions" comment.

## 3. Table export (CSV + JSON)

Tables rendering operator-facing data include export affordances in
the top-right of their container. Use the shared helper
`/static/js/table-export.js`.

**What to export**

What the operator *sees* — current page, current filters, current
sort. Don't silently pull unfiltered server data behind the
operator's back. Pagination caveat: if the operator is on page 3,
export gives them page 3. (Export-all-pages is a future enhancement,
not this iteration.)

**Wiring pattern**

```html
<div id="nodes-export-buttons" class="table-export-buttons"></div>
```

```js
// After the DataTable first renders, replace the placeholder with
// live buttons that re-resolve the table selector on each click
// (so they still work after the table re-renders).
const host = document.getElementById('nodes-export-buttons');
host.replaceWith(
  MNMTableExport.makeButtons('#nodes-dt table', 'mnm-nodes')
);
```

Filename pattern: `mnm-<page>-<scope>-<YYYY-MM-DD>.<csv|json>`.
Examples: `mnm-nodes-2026-04-24.csv`,
`mnm-investigation-arp-2026-04-24.json`.

**Column-level export control**

- `exportLabel` on a DataTable column: override the CSV header text
  (e.g. strip icon glyphs from the visible label).
- `exportValue(v, row)` on a DataTable column: return a semantic
  string for cells whose rendered output is HTML markup (icons,
  badges). Without this, the exporter falls back to the raw `v`.

**Out of scope**

- Server-side export endpoints (all client-side).
- Paginate-all-pages export (flag as follow-up if operators ask).
- PDF, Excel, or any format beyond CSV + JSON.
- Column picker / filter dialog prior to export.

## 4. Shared helpers — where they live

| Helper | File | Purpose |
|---|---|---|
| `MNMColHelp.icon(opts)` | `static/js/col-help.js` | Header help icon + tooltip HTML |
| `MNMTableExport.*` | `static/js/table-export.js` | CSV / JSON export + button factory |
| `MNMIcons.*` | `static/js/icons.js` | Device-classification icon glyphs |
| `DataTable` | `static/js/datatable.js` | Paginated, sortable table component |

When adding a new page or a new table-bearing card, prefer extending
these helpers over hand-rolling another pattern. If a helper doesn't
fit, add to it or create a new one and document here before shipping.

## 5. When to extend this document

Update this file when you introduce a visual pattern that's reused
across ≥ 2 pages. Short and operator-facing — no design-system
over-engineering. If a convention change requires pages to retrofit,
call it out in `CHANGELOG.md` under Unreleased / Changed so the
operator knows which pages were touched.
