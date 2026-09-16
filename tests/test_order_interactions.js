// Run with: node tests/test_order_interactions.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const listeners = {};
const requests = [];
const chips = [];
const alerts = [];
let focused = false;
let confirmations = 0;
const project = {value: '', focus() { focused = true; }};
const form = {addEventListener(type, handler) { listeners.submit = handler; }};
const cell = {
  querySelectorAll() { return chips.map(chip => chip.child); },
  querySelector() { return {appendChild(chip) { chips.push(chip); }}; }
};
const input = {
  value: 'Known@lab.org', dataset: {},
  classList: {contains(value) { return value === 'tracker-input'; }},
  list: {options: [{value: 'known@lab.org'}]},
  closest() { return cell; }
};
const context = {
  document: {
    addEventListener(type, handler) { listeners[type] = handler; },
    getElementById(id) { return id === 'submit-orders' ? form : null; },
    querySelectorAll() { return [project]; },
    createElement() { return {dataset: {}, appendChild(child) { this.child = child; }}; }
  },
  window: {alert(message) { alerts.push(message); }, confirm() { confirmations++; return true; }},
  rowOf() { return {dataset: {id: '7'}}; },
  post(url, method, body, ok, fail) { requests.push({url, body, ok, fail}); }
};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function addChip('),
  source.indexOf('  /* --- column filters')), context);

listeners.input({target: input});
listeners.change({target: input});
context.addTracker(input); // Enter while suggestion request is pending.
assert.equal(requests.length, 1);
requests[0].ok({email: 'known@lab.org'});
assert.equal(chips.length, 1);
assert.equal(input.value, '');
input.value = 'known@lab.org';
listeners.input({target: input});
requests[1].ok({email: 'known@lab.org'});
assert.equal(chips.length, 1);

input.value = 'new@lab.org';
listeners.input({target: input});
assert.equal(requests.length, 2); // Unknown addresses still require Enter.
context.addTracker(input);
requests[2].fail();
assert.equal(input.value, 'new@lab.org');
context.addTracker(input);
assert.equal(requests.length, 4); // Failure allows retry.
input.value = 'another@lab.org';
requests[3].ok({email: 'new@lab.org'});
assert.equal(input.value, 'another@lab.org');

let prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(focused, true);
assert.equal(alerts[0], 'Select a project for every order before submitting.');
assert.equal(confirmations, 0);
project.value = '1';
prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, false);
assert.equal(confirmations, 1);
console.log('Order interaction checks passed');
