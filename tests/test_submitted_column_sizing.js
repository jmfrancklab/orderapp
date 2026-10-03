// Run with: node tests/test_submitted_column_sizing.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const widths = {};
const listeners = {};
const buyer = {textContent: 'a@lab.org'};
const project = {tagName: 'SELECT', selectedOptions: [{textContent: 'Small grant'}]};
const status = {tagName: 'SELECT', options: [
  {textContent: 'Not ready'}, {textContent: 'Awaiting Order'}
]};
const location = {value: 'Shelf A', hidden: false};
const fields = {
  '.item-key': [{textContent: '0x8f'}],
  '[data-field="user_email"]': [buyer],
  '[data-field="project_id"]': [project],
  '[data-field="order_status"]': [status]
};
const sheet = {
  style: {setProperty(name, value) { widths[name] = parseFloat(value); }},
  addEventListener(event, handler) { listeners[event] = handler; },
  querySelectorAll(selector) {
    const field = selector.replace('.submitted-row ', '');
    return field.startsWith('[data-field="location"]')
      ? (location.hidden ? [] : [location]) : fields[field] || [];
  }
};
const context = {
  submittedSheet: sheet,
  document: {
    documentElement: {},
    createElement() { return {getContext() {
      return {measureText(text) { return {width: text.length * 7}; }};
    }}; }
  },
  window: {getComputedStyle() {
    return {fontSize: '16px', font: '13px sans-serif', paddingLeft: '8', paddingRight: '8'};
  }}
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  /* Fit short Submitted columns'),
  source.indexOf('  /* --- lazy, read-only')), context);

assert.ok(widths['--key-width'] <= 48);
assert.equal(widths['--status-width'], 128); // Fit the longest available status.
const initialProjectWidth = widths['--project-width'];
project.selectedOptions = [{textContent: 'An extremely long project name'.repeat(10)}];
buyer.textContent = 'An extremely long email address'.repeat(10);
listeners.change();
assert.ok(widths['--project-width'] > initialProjectWidth);
assert.equal(widths['--project-width'], 192);
assert.equal(widths['--buyer-width'], 192); // Long values cannot crowd out Use.

location.value = 'A long storage location'.repeat(10);
listeners.input({target: {matches() { return true; }}});
assert.equal(widths['--location-width'], 160);
location.hidden = true;
listeners.change();
assert.equal(widths['--location-width'], 80); // Empty location columns stay compact.
console.log('Submitted column sizing checks passed');
