"""Public private-chat UI; Bot API transport + bot-authorized MTProto."""
import asyncio
import os
import secrets
import tempfile
import time
from pathlib import Path
from dotenv import load_dotenv
from telethon import TelegramClient
from telegram_api import BotAPI, ApiError
from export_core import authorized, target, chunks, line, write_files
from collector import bounded_collect

ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / '.env')
PROGRESS_EVERY_PAGES = 3
PROGRESS_EVERY_SECONDS = 15


def flood_text(seconds):
    return f'Telegram просит подождать ещё {seconds} секунд перед повторной выгрузкой этого чата.'


def progress_due(seen_pages, reported_pages, now, last_report):
    if seen_pages - reported_pages < PROGRESS_EVERY_PAGES:
        return False
    if last_report is not None and now - last_report < PROGRESS_EVERY_SECONDS:
        return False
    return True


class App:
    def __init__(self, api, mt, bot_id):
        self.api, self.mt, self.bot_id = api, mt, bot_id
        self.jobs = {}
        self.queue = asyncio.Semaphore(1)
        self.results = {}  # at most 20 users, expires after 10 minutes
        self.flood_until = {}  # chat id -> monotonic deadline; no automatic retry
        self.timeout = max(10, min(270, int(os.getenv('EXPORT_TIMEOUT', '240'))))
        self.max_members = max(1, min(100000, int(os.getenv('MAX_MEMBERS', '100000'))))

    async def rights(self, uid, chat_id):
        chat = await self.api.call('getChat', chat_id=chat_id)
        if chat['type'] not in ('group', 'supergroup', 'channel'):
            raise ValueError('Выберите группу или канал.')
        bot = await self.api.call('getChatMember', chat_id=chat['id'], user_id=self.bot_id)
        user = await self.api.call('getChatMember', chat_id=chat['id'], user_id=uid)
        if not authorized(bot['status']):
            raise ValueError('Сначала добавьте бота администратором выбранного чата.')
        if not authorized(user['status']):
            raise ValueError('Выгрузка доступна только владельцу или администратору выбранного чата.')
        return chat

    async def start(self, uid):
        keyboard = {'keyboard': [[{'text': 'Выбрать группу', 'request_chat': {
            'request_id': 1, 'chat_is_channel': False, 'bot_is_member': True}}],
            [{'text': 'Выбрать канал', 'request_chat': {
                'request_id': 2, 'chat_is_channel': True, 'bot_is_member': True}}]],
            'resize_keyboard': True}
        await self.api.send(uid,
            'Добавьте меня администратором в свою группу или канал, затем выберите чат кнопкой. '
            'Проверю ваши права администратора именно этого чата.\n'
            'Если кнопки недоступны, отправьте @username, https://t.me/username или числовой ID чата. '
            'Пригласительные ссылки не поддерживаются.\n'
            'Выгрузка: Excel, TXT и текст по кнопке. Telegram может отдать лишь часть списка. '
            'Обычно сбор ограничен четырьмя минутами, очередь и отправка файлов занимают дополнительное время.\n'
            '/cancel — отменить, /delete — удалить сохранённый текст.', reply_markup=keyboard)

    def prune(self):
        now = time.monotonic()
        self.results = {k: v for k, v in self.results.items() if v['expires'] > now}
        while len(self.results) > 20:
            del self.results[next(iter(self.results))]

    async def cleanup(self):
        while True:
            await asyncio.sleep(5)
            self.prune()

    def note_flood(self, chat_id, seconds):
        seconds = int(seconds)
        if seconds <= 0:
            return
        until = time.monotonic() + seconds
        self.flood_until[chat_id] = max(self.flood_until.get(chat_id, 0), until)

    def flood_left(self, chat_id):
        until = self.flood_until.get(chat_id)
        if not until:
            return 0
        left = until - time.monotonic()
        if left <= 0:
            self.flood_until.pop(chat_id, None)
            return 0
        return max(1, int(left + 0.999))

    async def export(self, uid, chat_id):
        try:
            async with self.queue:
                chat = await self.rights(uid, chat_id)  # recheck after queue
                left = self.flood_left(chat['id'])
                if left:
                    await self.api.send(uid, flood_text(left))
                    return
                await self.api.send(uid, 'Сбор начался. /cancel — отменить.')
                before = await self.api.call('getChatMemberCount', chat_id=chat['id'])
                rows = {}
                seen_pages = reported_pages = 0
                last_report = None

                async def on_progress(count):
                    nonlocal seen_pages, reported_pages, last_report
                    seen_pages += 1
                    now = time.monotonic()
                    if not progress_due(seen_pages, reported_pages, now, last_report):
                        return
                    reported_pages, last_report = seen_pages, now
                    try:
                        await self.api.send(uid, f'Получено {count}.')
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        pass

                def on_flood(seconds):
                    self.note_flood(chat['id'], seconds)

                reason = await bounded_collect(
                    self.mt, chat, rows, self.max_members, self.timeout,
                    on_progress=on_progress, on_flood=on_flood)
                # Enrich admin titles and roles from Bot API (including anonymous bots skipped there).
                admins = await self.api.call('getChatAdministrators', chat_id=chat['id'])
                for a in admins:
                    from export_core import Member
                    u = a['user']
                    rows[u['id']] = Member(' '.join(filter(None, [u.get('first_name'), u.get('last_name')])),
                        u.get('username', ''), u['id'], 'owner' if a['status'] == 'creator' else 'admin',
                        a.get('custom_title') or a.get('tag') or '')
                # Recheck immediately before disclosure; fail closed on rights/network errors.
                await self.rights(uid, chat['id'])
                after = await self.api.call('getChatMemberCount', chat_id=chat['id'])
                summary = (f"Чат: {chat.get('title', chat['id'])}\nПолучено {len(rows)}; общий счётчик Telegram: "
                    f'{before} до сбора, {after} после.\n'
                    'Результат может быть частичным. Совпадение чисел не доказывает полноту: состав меняется.\n'
                    + reason + '\nПодпись указана только там, где её предоставил Telegram. '
                    'Отсутствующее поле не означает, что подписи нет в Telegram.')
                members = sorted(rows.values(), key=lambda m: m.user_id)
                with tempfile.TemporaryDirectory(prefix='tg_export_') as folder:
                    txt, xlsx = await asyncio.to_thread(write_files, Path(folder), members, summary)
                    await self.api.send(uid, summary)
                    await self.api.document(uid, xlsx)
                    await self.api.document(uid, txt)
                self.prune()
                nonce = secrets.token_hex(6)
                # Build pages once, enforce TTL and ownership on every click.
                pages = list(chunks('\n'.join(line(m) for m in members))) or ['Участников не получено.']
                self.results[uid] = dict(chat_id=chat['id'], pages=pages,
                    nonce=nonce, expires=time.monotonic() + 600)
                self.prune()
                await self.api.send(uid, 'Текст доступен 10 минут; файлы уже отправлены.',
                    reply_markup={'inline_keyboard': [[{'text': 'Показать текст', 'callback_data': f't:{nonce}:0'}]]})
        except asyncio.CancelledError:
            await self.api.send(uid, 'Выгрузка отменена. Временные файлы удалены.')
            raise
        except ValueError as e:
            await self.api.send(uid, str(e))
        except Exception:
            await self.api.send(uid, 'Не удалось выполнить выгрузку или отправку. Проверьте права и повторите позже. '
                                'Если файлы уже получены, они содержат результат этой попытки.')
        finally:
            self.jobs.pop(uid, None)

    async def update(self, update):
        self.prune()
        cb = update.get('callback_query')
        if cb:
            await self.api.call('answerCallbackQuery', callback_query_id=cb['id'])
            uid = cb['from']['id']
            message = cb.get('message', {})
            if message.get('chat', {}).get('type') != 'private' or message['chat']['id'] != uid:
                return
            r = self.results.get(uid)
            parts = cb.get('data', '').split(':')
            if not r or len(parts) != 3 or parts[:2] != ['t', r['nonce']]:
                await self.api.send(uid, 'Срок хранения текста истёк. Запустите новую выгрузку.')
                return
            try:
                index = int(parts[2])
                if index < 0 or index >= len(r['pages']):
                    return
                await self.rights(uid, r['chat_id'])
            except (ValueError, ApiError):
                self.results.pop(uid, None)
                await self.api.send(uid, 'Не удалось подтвердить права. Текст удалён.')
                return
            extra = {}
            if index + 1 < len(r['pages']):
                extra['reply_markup'] = {'inline_keyboard': [[{'text': 'Далее',
                    'callback_data': f"t:{r['nonce']}:{index + 1}"}]]}
            await self.api.send(uid, f"Страница {index + 1}/{len(r['pages'])}\n" + r['pages'][index], **extra)
            return
        m = update.get('message', {})
        if m.get('chat', {}).get('type') != 'private' or not m.get('from'):
            return
        uid = m['from']['id']
        text = m.get('text', '').strip()
        if text in ('/start', '/help'):
            await self.start(uid)
        elif text in ('/cancel', '/delete'):
            self.results.pop(uid, None)
            job = self.jobs.get(uid)
            if job:
                job.cancel()
            else:
                await self.api.send(uid, 'Сохранённый текст удалён. Активной выгрузки нет.')
        else:
            if uid in self.jobs:
                await self.api.send(uid, 'Ваша выгрузка уже в очереди или выполняется. /cancel — отменить.')
                return
            if len(self.jobs) >= 10:
                await self.api.send(uid, 'Очередь заполнена. Повторите позже.')
                return
            try:
                chat_id = m['chat_shared']['chat_id'] if m.get('chat_shared') else target(text)
                chat = await self.rights(uid, chat_id)
                left = self.flood_left(chat['id'])
                if left:
                    await self.api.send(uid, flood_text(left))
                    return
                await self.api.send(uid, 'Права проверены. Заявка принята; впереди заявок: ' + str(len(self.jobs)))
                self.jobs[uid] = asyncio.create_task(self.export(uid, chat['id']))
            except ValueError as e:
                await self.api.send(uid, str(e))
            except ApiError:
                await self.api.send(uid, 'Не удалось проверить чат. Убедитесь, что вы и бот — его администраторы.')


async def main(check=False):
    token = os.getenv('BOT_TOKEN', '')
    if not token:
        print('Не заполнен BOT_TOKEN в .env.'); return
    api = BotAPI(token)
    mt = None
    app = None
    cleanup = None
    try:
        me = await api.call('getMe')
        webhook = await api.call('getWebhookInfo')
        print('Токен проверен. Бот: @' + me['username'])
        print('Webhook: ' + ('установлен (не изменён)' if webhook.get('url') else 'не установлен'))
        if check:
            print('MTProto: ' + ('данные приложения заполнены' if os.getenv('API_ID') and os.getenv('API_HASH') else 'нужны API_ID и API_HASH'))
            return
        if webhook.get('url'):
            print('Запуск остановлен: уже установлен webhook. Он не изменён.'); return
        if not os.getenv('API_ID') or not os.getenv('API_HASH'):
            print('Запуск остановлен: заполните API_ID и API_HASH в .env.'); return
        mt = TelegramClient(str(ROOT / 'work' / 'bot'), int(os.environ['API_ID']), os.environ['API_HASH'],
                            flood_sleep_threshold=0, request_retries=1, receive_updates=False)
        await mt.start(bot_token=token)
        mt_me = await mt.get_me()
        if not mt_me.bot or mt_me.id != me['id']:
            print('Запуск остановлен: сессия принадлежит другому аккаунту.'); return
        app = App(api, mt, me['id'])
        cleanup = asyncio.create_task(app.cleanup())
        offset = 0
        print('Бот запущен. Оставьте это окно открытым. Для остановки: Ctrl+C.')
        while True:
            try:
                updates = await api.call('getUpdates', offset=offset, timeout=25,
                                         allowed_updates=['message', 'callback_query'])
                for update in updates:
                    offset = update['update_id'] + 1
                    try:
                        await app.update(update)
                    except Exception:
                        print('Ошибка обработки обновления; подробности скрыты для защиты данных.')
            except ApiError as e:
                if e.code == 409:
                    print('Запуск остановлен: другой polling-процесс или webhook.'); break
                await asyncio.sleep(3)
    except ApiError as e:
        print('Проверка Telegram не удалась. Код: ' + str(e.code))
    except Exception:
        print('Не удалось запустить бот. Проверьте настройки и доступ к Telegram.')
    finally:
        if cleanup:
            cleanup.cancel()
            await asyncio.gather(cleanup, return_exceptions=True)
        if app:
            jobs = list(app.jobs.values())
            for task in jobs:
                task.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
        if mt:
            await mt.disconnect()
        await api.client.aclose()


if __name__ == '__main__':
    import sys
    (ROOT / 'work').mkdir(exist_ok=True)
    try:
        asyncio.run(main('--check' in sys.argv))
    except KeyboardInterrupt:
        print('Бот остановлен.')
