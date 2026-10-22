// Run with: node tests/test_receipt_report_interactions.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const block = source.slice(source.indexOf('  var reportActions ='), source.indexOf('  var filterSheet ='));

async function run(failure) {
  let click, requested, alerted, downloaded, revoked;
  const buttons = [{disabled: false, dataset: {receiptReport: 'project'}}, {disabled: false}];
  const status = {textContent: ''};
  const actions = {
    dataset: {reportUrl: '/invoices/receipt-report'},
    addEventListener: (_, callback) => { click = callback; },
    querySelectorAll: () => buttons,
    querySelector: () => status
  };
  const link = {click() {downloaded = this.download;}, remove() {}};
  class BrowserURL extends URL {}
  BrowserURL.createObjectURL = () => 'blob:report';
  BrowserURL.revokeObjectURL = url => {revoked = url;};
  vm.runInNewContext(block, {
    URL: BrowserURL,
    document: {getElementById: () => actions, createElement: () => link, body: {appendChild() {}}},
    window: {location: {origin: 'https://orders.example', search: '?filter_reimbursement_status=reimbursed&filter_reimbursement_status=requires+reimbursement'}, alert: message => {alerted = message;}},
    setTimeout: callback => callback(),
    fetch: async url => {
      requested = url;
      assert.ok(buttons.every(button => button.disabled));
      assert.equal(status.textContent, 'Preparing PDF…');
      return {ok: !failure, headers: {get: () => failure ? 'application/json' : 'application/pdf'},
        json: async () => ({message: 'Receipt #11 (Mixed purchase): provide a receipt for this row.'}),
        blob: async () => ({})};
    }
  });
  await click({target: {closest: () => buttons[0]}});
  assert.deepEqual(Array.from(requested.searchParams.getAll('filter_reimbursement_status')), ['reimbursed', 'requires reimbursement']);
  assert.equal(requested.searchParams.get('group_by'), 'project');
  assert.ok(buttons.every(button => !button.disabled));
  assert.equal(status.textContent, '');
  if (failure) {
    assert.match(alerted, /Receipt #11.*provide a receipt/);
    assert.equal(downloaded, undefined);
  } else {
    assert.equal(downloaded, 'receipts-by-project.pdf');
    assert.equal(revoked, 'blob:report');
    assert.equal(alerted, undefined);
  }
}
(async () => { await run(false); await run(true); console.log('Receipt report interactions passed'); })().catch(error => {console.error(error); process.exitCode = 1;});
