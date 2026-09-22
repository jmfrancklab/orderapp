// Run with: node tests/test_location_interactions.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('static/app.js', 'utf8');
const context = {};
vm.createContext(context);
vm.runInContext(source.slice(source.indexOf('  function locationDistance('),
  source.indexOf('  async function saveField(')), context);
let prompts = [];
let answers = [];
context.locationChoices = () => ['Basement shelf', 'on top of common desk', 'Storage closet'];
context.locationDialog = async (value, choices) => {
  prompts.push({value, choices});
  return answers.shift();
};
(async () => {
  let result = await context.chooseLocation(' ON TOP OF COMMON DESK ');
  assert.equal(result.location, 'on top of common desk');
  assert.equal(prompts.length, 0);
  answers = ['on top of common desk'];
  result = await context.chooseLocation('on top of comon desk');
  assert.equal(prompts[0].choices[0], 'on top of common desk');
  assert.equal(result.confirm_new_location, false);
  answers = ['New shelf'];
  result = await context.chooseLocation('New shelf');
  assert.equal(result.location, 'New shelf');
  assert.equal(result.confirm_new_location, true);
  answers = [null];
  assert.equal(await context.chooseLocation('Another shelf'), null);
  answers = ['Storage closet'];
  result = await context.chooseLocation('');
  assert.equal(result.location, 'Storage closet');
  assert.equal(prompts.at(-1).choices, null);
  answers = [null];
  assert.equal(await context.chooseLocation(''), null);
  vm.runInContext(source.slice(source.indexOf('  async function saveField('),
    source.indexOf('  function debounceSave(')), context);
  const status = {value: 'received', dataset: {field: 'order_status'},
    querySelector() { return {value: 'ordered'}; }};
  const location = {value: '', defaultValue: '', dataset: {field: 'location'}, disabled: true};
  const row = {dataset: {id: '26'}, querySelector(selector) {
    return selector.includes('location') ? location : status;
  }};
  context.rowOf = () => row;
  context.updateStatusClass = () => {};
  let request;
  context.post = (url, method, body, done) => { request = {url, body, done}; };
  context.chooseLocation = async () => null;
  await context.saveField(status);
  assert.equal(request, undefined);
  assert.equal(status.value, 'ordered');
  assert.equal(status.disabled, false);
  assert.equal(location.disabled, true);
  status.value = 'received';
  context.chooseLocation = async () => ({location: 'Storage closet'});
  await context.saveField(status);
  assert.equal(request.body.order_status, 'received');
  assert.equal(request.body.location, 'Storage closet');
  request.done({location: 'Storage closet'});
  assert.equal(location.value, 'Storage closet');
  assert.equal(location.required, true);
  assert.equal(location.disabled, false);
  assert.equal(status.dataset.savedValue, 'received');
  console.log('Location interaction checks passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
