// Run with: node tests/test_submitted_filter_interactions.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');

class Element {
  constructor() { this.children = []; this.dataset = {}; this.listeners = {}; }
  appendChild(child) { this.children.push(child); }
  addEventListener(type, handler) { this.listeners[type] = handler; }
  querySelector() { return null; }
  querySelectorAll() { return []; }
  remove() {}
}

function runAction(action) {
  const body = new Element();
  const sheet = new Element();
  sheet.dataset = {
    currentUser: 'viewer@lab.org',
    filterState: JSON.stringify({
      trackers: {selected: ['viewer@lab.org'], mode: 'or', scope: 'default'},
      description: {query: 'capacitor', regex: false}
    }),
    filterChoices: JSON.stringify({trackers: [
      {value: 'viewer@lab.org', label: 'viewer@lab.org'},
      {value: 'creator@lab.org', label: 'creator@lab.org'}
    ]})
  };
  let destination;
  const context = {
    URLSearchParams,
    document: {
      body,
      getElementById: id => id === 'submitted-sheet' ? sheet : null,
      createElement: () => new Element(),
      createTextNode: text => ({textContent: text})
    },
    window: {location: {
      pathname: '/submitted', search: '?filter_description=capacitor', hash: '',
      assign(url) { destination = url; }
    }}
  };
  vm.createContext(context);
  vm.runInContext(source.slice(source.indexOf('  function makePopupBtn('),
    source.indexOf('  function closeVendorPopup(')), context);
  vm.runInContext(source.slice(source.indexOf('  /* --- column filters'),
    source.indexOf('  /* --- lazy, read-only order details')), context);
  sheet.listeners.click({target: {closest() {
    return {dataset: {filterField: 'trackers'}};
  }}});
  const elements = [];
  function visit(element) {
    elements.push(element);
    (element.children || []).forEach(visit);
  }
  visit(body);
  const click = label => elements.find(el => el.textContent === label).onclick();
  if (action === 'uncheck') {
    const checkbox = elements.find(el => el.dataset?.filterValue === 'viewer@lab.org');
    checkbox.checked = false;
    checkbox.listeners.change();
    click('Apply');
  } else if (action === 'Select all') {
    click(action);
    click('Apply');
  } else {
    click(action);
  }
  return new URL(destination, 'http://localhost').searchParams;
}

for (const action of ['clear this filter', 'Clear all Filters', 'uncheck']) {
  const params = runAction(action);
  assert.deepEqual(params.getAll('tracker'), ['all'], action);
  assert.equal(params.get('filter_description'),
    action === 'Clear all Filters' ? null : 'capacitor', action);
}
// Selecting every named tracker still requires a matching tracker.
assert.deepEqual(runAction('Select all').getAll('tracker'),
  ['viewer@lab.org', 'creator@lab.org']);
console.log('Submitted filter interaction checks passed');
