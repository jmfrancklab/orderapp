// Run with: node tests/test_receipt_report_interactions.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const block = source.slice(source.indexOf('  var reportActions ='), source.indexOf('  var filterSheet ='));
const buttonHelper = source.slice(source.indexOf('  function makePopupBtn('), source.indexOf('  function closeVendorPopup('));

class Element {
  constructor(tag, document) {
    this.tag = tag; this.document = document; this.children = []; this.dataset = {};
    this.listeners = {}; this.disabled = false; this.value = ''; this.attributes = {};
  }
  appendChild(child) { child.parent = this; this.children.push(child); }
  remove() { this.parent.children = this.parent.children.filter(child => child !== this); }
  addEventListener(name, handler) { this.listeners[name] = handler; }
  setAttribute(name, value) { this.attributes[name] = value; }
  focus() { this.document.activeElement = this; }
  querySelectorAll(selector) {
    const tags = selector.split(',').map(value => value.trim());
    return this.children.flatMap(child => [child, ...child.querySelectorAll(selector)])
      .filter(child => tags.includes(child.tag));
  }
  reportValidity() { return true; }
}

async function run(mode) {
  let click, requested, downloaded, revoked, getCount = 0, posts = [];
  const document = {removeEventListener() {}, addEventListener() {}, querySelectorAll: () => []};
  document.createElement = tag => {
    const el = new Element(tag, document);
    if (tag === 'a') el.click = () => { downloaded = el.download; };
    return el;
  };
  document.body = document.createElement('body');
  const buttons = [document.createElement('button'), document.createElement('button')];
  buttons[0].dataset.receiptReport = 'project';
  document.activeElement = buttons[0];
  const status = {textContent: ''};
  const progress = {hidden: true, value: 0};
  let progressTick, timerCleared = false, now = 0;
  document.getElementById = () => ({
    dataset: {reportUrl: '/invoices/receipt-report', receiptCount: '13'},
    addEventListener: (_, callback) => {click = callback;},
    querySelectorAll: () => buttons, querySelector: selector => selector.includes("progress") ? progress : status
  });
  class BrowserURL extends URL {}
  BrowserURL.createObjectURL = () => 'blob:report';
  BrowserURL.revokeObjectURL = url => {revoked = url;};
  const errors = [11, 12].map(id => ({
    invoice_id: id, nickname: 'Purchase ' + id, kind: 'document', can_edit: true,
    message: 'Receipt #' + id + ': Could not download file.',
    source_field: id === 11 ? 'receipt_url' : 'invoice_url',
    source_url: 'https://files.example/bad-' + id + '.pdf'
  }));
  const context = {
    URL: BrowserURL, document, Date: {now: () => now},
    window: {
      location: {origin: 'https://orders.example', search: '?filter_reimbursement_status=reimbursed&filter_reimbursement_status=requires+reimbursement'},
      alert: () => {throw new Error('Native alerts must not be used');}
    },
    setTimeout: callback => callback(),
    setInterval: callback => { progressTick = callback; return 42; },
    clearInterval: id => { assert.equal(id, 42); timerCleared = true; },
    fetch: async (url, options) => {
      if (options.method === 'POST') {
        posts.push({url, body: JSON.parse(options.body)});
        return {ok: mode !== 'save-failure', json: async () => mode === 'save-failure' ? {error: 'Save failed'} : {ok: true}};
      }
      assert.equal(progress.hidden, false);
      assert.match(status.textContent, /remaining \(estimated\)/);
      progressTick();
      assert.equal(progress.value, 0);
      now += 300000;
      progressTick();
      assert.equal(progress.value, 95);
      assert.match(status.textContent, /taking longer than estimated/);
      requested = url; getCount++;
      const failure = mode !== 'success' && getCount === 1;
      return {ok: !failure, status: failure ? 500 : 200, headers: {get: () => failure ? 'application/json' : 'application/pdf'},
        json: async () => {
          if (mode === 'html-error') throw new Error('HTML response');
          return mode === 'generic-error' ? {message: 'Please reload and try again.'} : {message: 'Fix the links.', errors};
        },
        blob: async () => ({})};
    }
  };
  vm.runInNewContext(buttonHelper + block, context);
  await click({target: {closest: () => buttons[0]}});
  assert.deepEqual(Array.from(requested.searchParams.getAll('filter_reimbursement_status')), ['reimbursed', 'requires reimbursement']);
  assert.equal(requested.searchParams.get('group_by'), 'project');
  assert.ok(buttons.every(button => !button.disabled));
  assert.equal(status.textContent, '');
  assert.equal(progress.hidden, true);
  assert.equal(timerCleared, true);
  if (mode === 'success') {
    assert.equal(downloaded, 'receipts-by-project.pdf');
    assert.equal(revoked, 'blob:report');
    return;
  }
  const popup = document.body.children.find(child => child.attributes.role === 'dialog');
  assert.ok(popup);
  assert.equal(popup.attributes['aria-modal'], 'true');
  const inputs = popup.querySelectorAll('input');
  const form = popup.children.find(child => child.tag === 'form');
  if (mode === 'generic-error' || mode === 'html-error') {
    assert.equal(inputs.length, 0);
    assert.ok(!form.children.some(child => /Correct the links/.test(child.textContent)));
    assert.ok(form.children.some(child => /no links have been identified/.test(child.textContent)));
    if (mode === 'html-error') {
      assert.ok(form.children.some(child => /HTTP 500.*PythonAnywhere error log/.test(child.textContent)));
    }
    return;
  }
  // Only the two offending URLs, with no extra invoice or receipt fields.
  assert.equal(inputs.length, 2);
  assert.deepEqual(inputs.map(input => input.value), errors.map(error => error.source_url));
  inputs[0].value = 'https://files.example/fixed-11.pdf';
  inputs[1].value = 'https://files.example/fixed-12.pdf';
  await form.listeners.submit({preventDefault() {}});
  assert.deepEqual(posts[0], {url: '/api/invoices/11', body: {receipt_url: inputs[0].value}});
  if (mode === 'save-failure') {
    assert.equal(getCount, 1);
    assert.equal(downloaded, undefined);
    assert.ok(document.body.children.includes(popup));
    assert.ok(form.children.some(child => /Receipt #11: Save failed/.test(child.textContent)));
    assert.ok(inputs.every(input => !input.disabled));
    return;
  }
  assert.deepEqual(posts[1], {url: '/api/invoices/12', body: {invoice_url: inputs[1].value}});
  assert.equal(getCount, 2);
  assert.equal(downloaded, 'receipts-by-project.pdf');
  assert.ok(!document.body.children.includes(popup));
}
(async () => {
  for (const mode of ['success', 'failure', 'save-failure', 'generic-error', 'html-error']) await run(mode);
  console.log('Receipt report interactions passed');
})().catch(error => {console.error(error); process.exitCode = 1;});
