const { execFile } = require('child_process');
const { promisify } = require('util');
const path = require('path');

const execFileAsync = promisify(execFile);
const TRIGGER = /(?:^|[^\p{L}\p{N}_])(?:скинь(?:те)?|кинь(?:те)?|дай(?:те)?|пришли(?:те)?|пришлите|отправь(?:те)?)\s+(?:мне\s+)?(?:дз|домашк[ау]|домашнее(?:\s+задание)?)(?=$|[^\p{L}\p{N}_])/iu;
const COOLDOWN_MS = 30 * 1000;

// Выполняется в браузере. Не загружаем участников и последнее сообщение:
// getChatModel в whatsapp-web.js 1.34.7 падает на новом формате MessageKey ($1).
function readCachedGroupName(chatId) {
  const chat = window.require('WAWebCollections').Chat.get(chatId);
  return chat ? chat.formattedTitle || chat.name || null : null;
}

async function fetchHomework(requestedAt) {
  const projectRoot = path.resolve(__dirname, '../..');
  const { stdout } = await execFileAsync(
    process.env.PYTHON || 'python3',
    ['-m', 'src.dnevnik.fetcher', '--at', String(requestedAt / 1000)],
    { cwd: projectRoot, timeout: 60000, env: process.env },
  );
  const text = stdout.trim();
  if (!text) throw new Error('Фетчер не вернул текст ДЗ');
  return text;
}

function createMessageHandler({ client, groupName = '', groupId = '', fetch = fetchHomework, now = Date.now, logger = console }) {
  let lastSent = null;
  let busy = false;
  let lastReplyText = null;

  return async (msg) => {
    // Сохраняем время вопроса до любых асинхронных операций и запроса API.
    const requestedAt = Number.isFinite(msg.timestamp) && msg.timestamp > 0
      ? msg.timestamp * 1000 : now();
    const body = msg.body || '';
    if (!TRIGGER.test(body) || (msg.fromMe && (body === lastReplyText || body.startsWith('📚 ДЗ на ')))) return;
    const chatId = msg.fromMe ? msg.to : msg.from;
    if (typeof chatId !== 'string' || !chatId.endsWith('@g.us')) return;
    if (!groupId && !groupName) return;

    try {
      if (groupId) {
        if (chatId !== groupId) return;
      } else {
        const name = await client.pupPage.evaluate(readCachedGroupName, chatId);
        if (name !== groupName) return;
      }

      if (busy || (lastSent !== null && now() - lastSent < COOLDOWN_MS)) return;
      busy = true;
      try {
        let text;
        try {
          text = await fetch(requestedAt);
        } catch (error) {
          logger.error('Ошибка получения ДЗ:', error.stderr || error.message || error);
          text = 'Не получилось вытащить ДЗ из дневника. Попробуйте позже.';
        }
        // Обычное сообщение в ту же группу: не зависит от сериализации
        // ID исходного сообщения для цитирования и не отмечает чат прочитанным.
        lastReplyText = text;
        await client.sendMessage(chatId, text, { sendSeen: false });
        lastSent = now();
      } finally {
        busy = false;
      }
    } catch (error) {
      logger.error('Не удалось обработать запрос ДЗ:', error.message || error);
    }
  };
}

module.exports = { createMessageHandler, readCachedGroupName };
