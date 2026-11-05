// WhatsApp-бот "скиньте дз" на whatsapp-web.js (неофициальный клиент).
// Запуск: npm install && npm start (первый раз — отсканировать QR со своего телефона).
const path = require('path');
const projectRoot = path.resolve(__dirname, '../..');
require('dotenv').config({ path: path.join(projectRoot, '.env') });
const { Client, LocalAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const { createMessageHandler } = require('./message-handler');

const GROUP_NAME = (process.env.GROUP_NAME || '').trim();
const GROUP_ID = (process.env.GROUP_ID || '').trim();
const TIMEZONE = process.env.BOT_TIMEZONE || 'Europe/Moscow';

if (!GROUP_ID && !GROUP_NAME) {
  console.error('Задайте GROUP_ID или точное GROUP_NAME в .env.');
  process.exit(1);
}
if (GROUP_ID && !/^\d+(?:-\d+)?@g\.us$/.test(GROUP_ID)) {
  console.error('GROUP_ID должен быть ID группы WhatsApp и заканчиваться на @g.us.');
  process.exit(1);
}
if (!process.env.DNEVNIK_TOKEN) {
  console.error('Задайте DNEVNIK_TOKEN в .env.');
  process.exit(1);
}
try {
  new Intl.DateTimeFormat('ru-RU', { timeZone: TIMEZONE }).format();
} catch (_) {
  console.error('Неизвестный BOT_TIMEZONE; пример: Europe/Moscow.');
  process.exit(1);
}

const client = new Client({
  authStrategy: new LocalAuth({ dataPath: path.join(projectRoot, '.wwebjs_auth') }),
  webVersionCache: { type: 'local', path: path.join(projectRoot, '.wwebjs_cache') },
  puppeteer: { headless: true, args: ['--no-sandbox', '--disable-setuid-sandbox'] },
});

client.on('qr', (qr) => {
  console.log('Отсканируй QR в WhatsApp: Настройки → Связанные устройства:');
  qrcode.generate(qr, { small: true });
});
client.on('authenticated', () => console.log('Вход подтверждён. Жду загрузки WhatsApp и обработчиков сообщений…'));
client.on('loading_screen', (percent) => console.log(`Загрузка WhatsApp: ${percent}%`));
client.on('change_state', (state) => console.log('Состояние WhatsApp:', state));
client.on('ready', () => {
  console.log('Бот на связи. Группа:', GROUP_ID || GROUP_NAME);
  console.log(`До 10:00 — ДЗ на сегодня, с 10:00 — на завтра (${TIMEZONE}). Пауза: 30 секунд.`);
});
client.on('auth_failure', (m) => console.error('Ошибка авторизации:', m));
client.on('disconnected', (r) => console.error('Отключено:', r));

// message_create включает команды, отправленные с аккаунта самого бота.
client.on('message_create', createMessageHandler({ client, groupName: GROUP_NAME, groupId: GROUP_ID }));

// Ошибки в странице и асинхронном старте иначе могут остаться без вывода.
let observedPage;
function observePage() {
  if (!client.pupPage || observedPage === client.pupPage) return;
  observedPage = client.pupPage;
  observedPage.on('pageerror', (error) => console.error('Ошибка WhatsApp Web:', error.message));
  observedPage.on('error', (error) => console.error('Ошибка страницы браузера:', error.message));
}
const pageObserver = setInterval(observePage, 250);
pageObserver.unref();
console.log('Запускаю браузер и подключаюсь к WhatsApp…');
client.initialize().then(() => {
  observePage();
  clearInterval(pageObserver);
}).catch(async (error) => {
  clearInterval(pageObserver);
  console.error('Не удалось запустить бота:', error.message || error);
  try { await client.destroy(); } catch (_) { /* Браузер мог уже закрыться. */ }
  process.exitCode = 1;
});
