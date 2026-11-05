const { test } = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const { createMessageHandler } = require('../src/whatsapp/message-handler');

function setup(options = {}) {
  const sent = [];
  const errors = [];
  let lookups = 0;
  let fetches = 0;
  let time = 1000;
  const client = {
    pupPage: {
      async evaluate(fn, id) {
        lookups++;
        const chat = { formattedTitle: 'ТестБота' };
        Object.defineProperty(chat, 'lastReceivedKey', { get() { throw new Error('Broken message ID'); } });
        const window = { require: () => ({ Chat: { get: (key) => {
          assert.equal(key, id);
          return chat;
        } } }) };
        return vm.runInNewContext(`(${fn.toString()})(chatId)`, { window, chatId: id });
      },
    },
    async sendMessage(...args) { sent.push(args); return {}; },
  };
  const handler = createMessageHandler({
    client,
    groupName: 'ТестБота',
    fetch: async () => { fetches++; return 'ДЗ из дневника'; },
    now: () => time,
    logger: { error: (...args) => errors.push(args) },
    ...options,
  });
  const msg = {
    from: '123@g.us', body: 'скиньте дз', fromMe: false, id: { $1: 'new-message-key' },
    getChat() { throw new Error('getChat must not be called'); },
    reply() { throw new Error('reply must not be called'); },
  };
  return { handler, client, msg, sent, errors, counts: () => ({ lookups, fetches }), advance: (ms) => { time += ms; } };
}

test('responds using only cached group name, without broken chat serialization or quoting', async () => {
  const s = setup();
  await s.handler(s.msg);
  assert.deepEqual(s.sent, [['123@g.us', 'ДЗ из дневника', { sendSeen: false }]]);
  assert.equal(s.errors.length, 0);
});

test('ignores messages without a group destination, unrelated text, personal chats and other group IDs before browser lookup', async () => {
  const s = setup({ groupId: '123@g.us' });
  for (const override of [{ fromMe: true }, { body: 'привет' }, { from: '123@c.us' }, { from: '456@g.us' }]) {
    await s.handler({ ...s.msg, ...override });
  }
  assert.deepEqual(s.counts(), { lookups: 0, fetches: 0 });
  assert.equal(s.sent.length, 0);
});

test('accepts commands from the bot account and replies to the group in msg.to', async () => {
  const s = setup();
  await s.handler({ ...s.msg, fromMe: true, from: 'bot@c.us', to: '123@g.us' });
  assert.deepEqual(s.sent, [['123@g.us', 'ДЗ из дневника', { sendSeen: false }]]);
  assert.equal(s.errors.length, 0);
});

test('ignores its own homework reply even when it contains a trigger and arrives after cooldown', async () => {
  const text = '📚 ДЗ на завтра:\n1. Русский — написать фразу «скиньте дз».';
  const s = setup({ fetch: async () => text });
  const echo = { ...s.msg, fromMe: true, from: 'bot@c.us', to: '123@g.us', body: text };
  s.client.sendMessage = async (...args) => {
    s.sent.push(args);
    await s.handler(echo);
  };
  await s.handler({ ...s.msg, fromMe: true, from: 'bot@c.us', to: '123@g.us' });
  s.advance(30 * 1000);
  await s.handler(echo);
  assert.equal(s.sent.length, 1);
});

test('group ID takes priority and does not need a browser chat lookup', async () => {
  const s = setup({ groupId: '123@g.us', groupName: 'Другое название' });
  await s.handler(s.msg);
  assert.equal(s.counts().lookups, 0);
  assert.equal(s.sent.length, 1);
});

test('different or missing group names cannot trigger a response', async () => {
  const s = setup({ groupName: 'Другая группа' });
  await s.handler(s.msg);
  assert.equal(s.counts().fetches, 0);
  s.client.pupPage.evaluate = async () => null;
  await s.handler(s.msg);
  assert.equal(s.counts().fetches, 0);
  const noGroup = setup({ groupName: '' });
  await noGroup.handler(noGroup.msg);
  assert.equal(noGroup.sent.length, 0);
});

test('cooldown expires exactly 30 seconds after a successful send', async () => {
  const s = setup();
  await s.handler(s.msg);
  await s.handler(s.msg);
  assert.equal(s.sent.length, 1);
  s.advance(29999);
  await s.handler(s.msg);
  assert.equal(s.sent.length, 1);
  s.advance(1);
  await s.handler(s.msg);
  assert.equal(s.sent.length, 2);
});

test('uses the original message timestamp even when processing crosses 10:00', async () => {
  const questionTime = Date.parse('2026-11-04T06:59:59Z');
  const received = [];
  const s = setup({
    now: () => Date.parse('2026-11-04T07:00:01Z'),
    fetch: async (at) => { received.push(at); return 'ДЗ'; },
  });
  await s.handler({ ...s.msg, timestamp: questionTime / 1000 });
  assert.deepEqual(received, [questionTime]);
});

test('falls back to reception time when message timestamp is missing', async () => {
  const received = [];
  const s = setup({ fetch: async (at) => { received.push(at); return 'ДЗ'; } });
  await s.handler(s.msg);
  assert.deepEqual(received, [1000]);
});

test('recognizes request variants and preserves URLs in sent text', async () => {
  for (const body of ['Скиньте дз', 'кинь мне дз!', 'Пришлите домашнее задание', 'дай домашку']) {
    const text = '📚 ДЗ на четверг, 5 ноября 2026:\n1. Химия — https://example.org/?a=1&b=2';
    const s = setup({ fetch: async () => text });
    await s.handler({ ...s.msg, body });
    assert.equal(s.sent[0][1], text);
  }
  const s = setup();
  await s.handler({ ...s.msg, body: 'непришлите дз' });
  await s.handler({ ...s.msg, body: 'скинь дзюдо' });
  assert.equal(s.sent.length, 0);
});

test('an older homework reply cannot trigger a loop after another response', async () => {
  const oldText = '📚 ДЗ на среду, 4 ноября 2026:\n1. Русский — написать «скиньте дз».';
  const s = setup();
  await s.handler(s.msg);
  s.advance(30000);
  await s.handler({ ...s.msg, body: oldText, fromMe: true, to: '123@g.us' });
  assert.equal(s.sent.length, 1);
});

test('a pending fetch prevents simultaneous responses', async () => {
  let finish;
  const s = setup({ fetch: () => new Promise(resolve => { finish = resolve; }) });
  const first = s.handler(s.msg);
  await new Promise(resolve => setImmediate(resolve));
  await s.handler(s.msg);
  finish('ДЗ');
  await first;
  assert.equal(s.sent.length, 1);
});

test('fetch errors produce a controlled error message', async () => {
  const s = setup({ fetch: async () => { throw new Error('API unavailable'); } });
  await s.handler(s.msg);
  assert.match(s.sent[0][1], /Не получилось/);
  assert.equal(s.errors.length, 1);
});

test('send rejection is caught and does not block the next attempt', async () => {
  const s = setup();
  s.client.sendMessage = async () => { throw new Error('Browser send failed'); };
  await assert.doesNotReject(() => s.handler(s.msg));
  assert.equal(s.errors.length, 1);
  s.client.sendMessage = async (...args) => { s.sent.push(args); };
  await s.handler(s.msg);
  assert.equal(s.sent.length, 1);
});
