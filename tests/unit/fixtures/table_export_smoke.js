// Smoke-test runner for table-export.js — invoked from test_table_export_js.py
// via `node this.js path/to/table-export.js`.
//
// Fixture: 3 cols x 2 rows, including an embedded comma and an embedded
// double-quote so the RFC 4180 quoting branches are exercised.
global.window = undefined;
const exp = require(process.argv[2]);

const headers = ['Name', 'IP', 'Notes'];
const rows = [
  ['alpha', '192.0.2.1', 'lab, main'],
  ['beta',  '192.0.2.2', 'vEOS "lab"'],
];

const csv = exp.rowsToCSV(headers, rows);
const obj = exp.rowsToJSON(headers, rows);

const lines = csv.split('\r\n');
if (lines.length !== 3) { console.error('csv lines', lines.length); process.exit(1); }
if (lines[0] !== 'Name,IP,Notes') { console.error('header', lines[0]); process.exit(1); }
// Embedded comma — field must be quoted.
const expectedRow1 = 'alpha,192.0.2.1,"lab, main"';
if (lines[1] !== expectedRow1) { console.error('row1', lines[1]); process.exit(1); }
// Embedded double-quote — doubled inside quoted field.
const expectedRow2 = 'beta,192.0.2.2,"vEOS ""lab"""';
if (lines[2] !== expectedRow2) { console.error('row2', lines[2]); process.exit(1); }

if (!Array.isArray(obj) || obj.length !== 2) { console.error('json shape'); process.exit(1); }
if (obj[0].Name !== 'alpha' || obj[0].IP !== '192.0.2.1' || obj[0].Notes !== 'lab, main') {
  console.error('json row 0', obj[0]); process.exit(1);
}
if (obj[1].Notes !== 'vEOS "lab"') { console.error('json row 1', obj[1]); process.exit(1); }

process.stdout.write(JSON.stringify({ csv: csv, json: obj }));
