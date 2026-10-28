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
  const progress = {hidden: true, value: 0, removeAttribute() { this.value = undefined; }};
  let snapshots = [];
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
    URL: BrowserURL, document, TextDecoder, Uint8Array, Blob,
    window: {
      location: {origin: 'https://orders.example', search: '?filter_reimbursement_status=reimbursed&filter_reimbursement_status=requires+reimbursement'},
      alert: () => {throw new Error('Native alerts must not be used');}
    },
    setTimeout: callback => callback(),
    fetch: async (url, options) => {
      if (options.method === 'POST') {
        posts.push({url, body: JSON.parse(options.body)});
        return {ok: mode !== 'save-failure', json: async () => mode === 'save-failure' ? {error: 'Save failed'} : {ok: true}};
      }
      assert.equal(progress.hidden, false);
      assert.match(status.textContent, /loading purchases/);
      requested = url; getCount++;
      const failure = mode !== 'success' && getCount === 1;
      return {ok: !failure, status: failure ? 500 : 200, headers: {get: () => failure ? 'application/json' : 'application/vnd.orderapp.receipt-report'},
        json: async () => {
          if (mode === 'html-error') throw new Error('HTML response');
          return mode === 'generic-error' ? {message: 'Please reload and try again.'} : {message: 'Fix the links.', errors};
        },
        body: { getReader() {
          const pdf = Buffer.from('%PDF-test');
          const records = [
            {type: 'progress', label: 'Reading document pages', percent: 35, completed: 2, total: 7, remaining_seconds: 20},
            {type: 'progress', label: 'Embedding supporting pages', percent: 94, completed: 5, total: 6, remaining_seconds: 2},
            {type: 'pdf', size: pdf.length}
          ].map(record => JSON.stringify(record) + '\n').join('');
          const data = Buffer.concat([Buffer.from(records), pdf]);
          let offset = 0;
          return {
            async read() {
              snapshots.push(status.textContent);
              if (offset === data.length) return {done: true};
              // Split JSON records and the PDF at arbitrary network boundaries.
              const value = new Uint8Array(data.subarray(offset, offset + 17));
              offset += value.length;
              return {done: false, value};
            },
            async cancel() {}, releaseLock() {}
          };
        }}
      };
    }
  };
  vm.runInNewContext(buttonHelper + block, context);
  await click({target: {closest: () => buttons[0]}});
  assert.deepEqual(Array.from(requested.searchParams.getAll('filter_reimbursement_status')), ['reimbursed', 'requires reimbursement']);
  assert.equal(requested.searchParams.get('group_by'), 'project');
  assert.equal(requested.searchParams.get('progress'), '1');
  assert.ok(buttons.every(button => !button.disabled));
  assert.equal(status.textContent, '');
  assert.equal(progress.hidden, true);
  if (mode === 'success') {
    assert.ok(snapshots.some(text => /Reading document pages… 2 of 7.*20 seconds/.test(text)));
    assert.ok(snapshots.some(text => /Embedding supporting pages… 5 of 6/.test(text)));
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

// Exercise byte framing independently of the surrounding popup interactions.
(async function testStreamReader() {
  const readerSource = source.slice(source.indexOf('  async function readReceiptReport('),
    source.indexOf('  async function downloadReceiptReport('));
  const context = {TextDecoder, Uint8Array, Blob};
  vm.runInNewContext(readerSource, context);
  function response(data, split) {
    let offset = 0;
    const reader = {
      cancelled: false, released: false,
      async read() {
        if (offset === data.length) return {done: true};
        const value = new Uint8Array(data.subarray(offset, offset + split));
        offset += value.length;
        return {done: false, value};
      },
      async cancel() {this.cancelled = true;},
      releaseLock() {this.released = true;}
    };
    return {body: {getReader: () => reader}, reader};
  }
  const pdf = Buffer.from('%PDF-1.7\n\x00\xff\x80 binary\n');
  const header = Buffer.from(JSON.stringify({type: 'progress', label: 'Reading…', percent: 40}) + '\n'
    + JSON.stringify({type: 'pdf', size: pdf.length}) + '\n');
  for (const split of [1, 7, 10000]) {
    const input = response(Buffer.concat([header, pdf]), split);
    const progress = [];
    const blob = await context.readReceiptReport(input, event => progress.push(event));
    assert.deepEqual(Buffer.from(await blob.arrayBuffer()), pdf);
    assert.equal(progress[0].label, 'Reading…');
    assert.equal(input.reader.released, true);
  }
  const failed = response(Buffer.from(JSON.stringify({type: 'error', message: 'Fix links',
    errors: [{invoice_id: 11}]}) + '\n'), 3);
  await assert.rejects(context.readReceiptReport(failed, () => {}), error => {
    assert.equal(error.message, 'Fix links');
    assert.equal(error.reportErrors[0].invoice_id, 11);
    return true;
  });
  assert.ok(failed.reader.cancelled && failed.reader.released);
  for (const data of [header, Buffer.concat([header, pdf.subarray(0, 5)])]) {
    await assert.rejects(context.readReceiptReport(response(data, 5), () => {}), /interrupted/);
  }
})().catch(error => {console.error(error); process.exitCode = 1;});

// Fetched unit costs can include sub-cent tariff allocations.
{
  const formatSource = source.slice(source.indexOf('  function fmtCurrency('),
    source.indexOf('  function setCostInput('));
  const context = {};
  vm.runInNewContext(formatSource, context);
  assert.equal(context.fmtCurrency('0.64667'), '0.64667');
  assert.equal(context.fmtCurrency('1234.56'), '1,234.56');
}
