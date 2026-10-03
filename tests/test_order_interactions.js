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
const otherProject = {value: ''};
const checkbox = {checked: true, addEventListener(type, handler) { this.onchange = handler; },
  closest() { return {querySelector() { return project; }}; }};
const unchecked = {checked: false, addEventListener() {},
  closest() { return {querySelector() { return otherProject; }}; }};
let liveDrafts = [checkbox, unchecked];
const selectAll = {checked: false, addEventListener(type, handler) { this.onchange = handler; }};
const submitButton = {};
const mode = {value: 'all'};
const toggle = {addEventListener(type, handler) { this.onclick = handler; },
  setAttribute(name, value) { this[name] = value; }};
const selectionControls = {};
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
    getElementById(id) { return {'submit-orders': form, 'select-all-drafts': selectAll,
      'submit-selected-orders': submitButton, 'draft-submission-mode': mode,
      'select-for-submission': toggle, 'draft-selection-controls': selectionControls}[id] || null; },
    querySelectorAll(selector) {
      return selector === '.draft-select:checked'
        ? liveDrafts.filter(item => item.checked) : liveDrafts;
    },
    createElement() { return {dataset: {}, appendChild(child) { this.child = child; }}; }
  },
  window: {alert(message) { alerts.push(message); }, confirm() { confirmations++; return true; }},
  rowOf(target) { return target.row || {dataset: {id: '7'}}; },
  updateOrderTotals() {},
  updateSubmittedColumnWidths() {},
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

assert.equal(submitButton.textContent, 'Submit all');
assert.equal(submitButton.disabled, false);
assert.equal(checkbox.hidden, true);
assert.equal(checkbox.disabled, true);
assert.equal(selectionControls.hidden, true);
assert.equal(toggle.textContent, 'Select for Submission');
assert.equal(toggle['aria-pressed'], 'false');

let prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(focused, true);
assert.equal(alerts[0], 'Select a project for every order before submitting.');
assert.equal(confirmations, 0);
project.value = '1';
otherProject.value = '1';
prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, false);
assert.equal(confirmations, 1);

// Selection is opt-in and validates only checked rows.
toggle.onclick();
assert.equal(mode.value, 'selected');
assert.equal(checkbox.hidden, false);
assert.equal(checkbox.disabled, false);
assert.equal(checkbox.checked, false);
assert.equal(submitButton.disabled, true);
assert.equal(selectionControls.hidden, false);
assert.equal(toggle['aria-pressed'], 'true');
assert.equal(toggle.textContent, 'Cancel selection');
checkbox.checked = true;
checkbox.onchange();
otherProject.value = '';
prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, false);
assert.equal(confirmations, 2);
assert.equal(selectAll.indeterminate, true);
selectAll.checked = true;
selectAll.onchange();
assert.equal(unchecked.checked, true);
assert.equal(selectAll.indeterminate, false);
assert.equal(submitButton.textContent, 'Submit selected rows (2)');
selectAll.checked = false;
selectAll.onchange();
assert.equal(submitButton.disabled, true);
prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(confirmations, 2);

// Leaving selection mode restores submission of every row.
toggle.onclick();
assert.equal(mode.value, 'all');
assert.equal(submitButton.textContent, 'Submit all');
assert.equal(submitButton.disabled, false);
assert.equal(checkbox.hidden, true);
assert.equal(selectionControls.hidden, true);
assert.equal(toggle['aria-pressed'], 'false');

// Run the actual AJAX deletion callback: detached checkboxes must not count.
toggle.onclick();
checkbox.checked = true;
checkbox.onchange();
const deletionStart = source.indexOf('    // confirm delete');
vm.runInContext('function deleteDraft(e) {\n' +
  source.slice(deletionStart, source.indexOf('\n  });', deletionStart)) + '\n}', context);
const row = {dataset: {id: '7'}, remove() { liveDrafts = [unchecked]; }};
context.deleteDraft({target: {row, classList: {contains(name) { return name === 'del-yes'; }}}});
requests.at(-1).ok();
assert.equal(submitButton.textContent, 'Submit selected rows (0)');
assert.equal(submitButton.disabled, true);
assert.equal(selectAll.indeterminate, false);
prevented = false;
listeners.submit({preventDefault() { prevented = true; }});
assert.equal(prevented, true);
assert.equal(confirmations, 2);
selectAll.checked = true;
selectAll.onchange();
assert.equal(submitButton.textContent, 'Submit selected rows (1)');
assert.equal(selectAll.indeterminate, false);
console.log('Order interaction checks passed');
