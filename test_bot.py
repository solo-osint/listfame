import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch
from openpyxl import load_workbook
from telethon import types, errors
from bot import App
from collector import bounded_collect, collect, member
from export_core import Member, authorized, target, write_files, chunks


class CoreTests(unittest.TestCase):
    def test_auth_and_target(self):
        for status in ['member', 'restricted', 'left', 'kicked', '', None]:
            self.assertFalse(authorized(status))
        self.assertTrue(authorized('creator'))
        self.assertTrue(authorized('administrator'))
        self.assertEqual(target('https://t.me/test_chat'), '@test_chat')
        self.assertEqual(target('-100123456'), -100123456)
        for value in ['https://t.me/+secret', '123', '@x', 'https://evil.com/a']:
            with self.assertRaises(ValueError):
                target(value)

    def test_files_formula_and_id(self):
        with tempfile.TemporaryDirectory() as d:
            txt, xlsx = write_files(Path(d), [Member('=1+1\x00', '+cmd', 9007199254740993, 'admin', '=WEBSERVICE("x")')], 'Частичный результат')
            wb = load_workbook(xlsx)
            ws = wb['Участники']
            for c in ws[2]:
                self.assertEqual(c.data_type, 's')
            self.assertEqual(ws['A2'].value, '=1+1')
            self.assertEqual(ws['C2'].value, '9007199254740993')
            self.assertIn('Частичный', txt.read_text(encoding='utf-8-sig'))

    def test_unicode_pages(self):
        text = '😀А' * 3000
        pages = list(chunks(text))
        self.assertEqual(''.join(pages), text)
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3500 for p in pages))

    def test_member_tag(self):
        u = NS(first_name='Имя', last_name='', username=None, id=1)
        p = types.ChannelParticipant(1, None, rank='Дизайнер')
        self.assertEqual(member(u, p).title, 'Дизайнер')
        self.assertEqual(member(u, p).role, 'member')


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_end_to_end_files_and_text_ownership(self):
        api = NS(send=AsyncMock(), call=AsyncMock(side_effect=[2, [], 2]))
        files = []
        async def document(uid, path):
            self.assertEqual(uid, 1)
            self.assertTrue(path.is_file())
            files.append(path)
        api.document = document
        app = App(api, None, 99)
        app.rights = AsyncMock(return_value={'id': -100123, 'type': 'channel', 'title': 'Тест'})
        async def fake_collect(client, chat, rows, *args):
            rows[7] = Member('Имя', 'user', 7, 'member', 'Дизайнер')
            return 'Тестовая частичная выдача'
        with patch('bot.bounded_collect', fake_collect):
            await app.export(1, -100123)
        self.assertEqual(len(files), 2)
        self.assertTrue(all(not p.exists() for p in files))
        result = app.results[1]
        self.assertIn('Дизайнер', result['pages'][0])
        # Another user cannot retrieve this user's cached text with a known nonce.
        before = app.rights.await_count
        api.call = AsyncMock()
        await app.update({'callback_query': {'id': 'x', 'from': {'id': 2},
            'message': {'chat': {'id': 2, 'type': 'private'}},
            'data': 't:' + result['nonce'] + ':0'}})
        self.assertEqual(before, app.rights.await_count)
        self.assertNotIn(2, app.results)

    async def test_rights_fail_closed(self):
        for bot_status, user_status in [('member', 'creator'), ('administrator', 'member')]:
            api = NS(call=AsyncMock(side_effect=[{'id': -1001, 'type': 'channel'},
                {'status': bot_status}, {'status': user_status}]))
            with self.assertRaises(ValueError):
                await App(api, None, 99).rights(1, -1001)

    async def test_dedup_and_repeated_page(self):
        user = NS(id=1, first_name='A', last_name='', username='a')
        page = NS(users=[user], participants=[types.ChannelParticipant(1, None)])
        client = AsyncMock(return_value=page)
        rows = {}
        reason = await collect(client, {'id': -100123, 'type': 'channel'}, rows, 100)
        self.assertEqual(len(rows), 1)
        self.assertIn('повторил', reason)

    async def test_timeout_retains_rows(self):
        rows = {}
        async def slow(*args):
            rows[1] = Member('A', '', 1, 'member')
            await asyncio.sleep(1)
        with patch('collector.collect', slow):
            reason = await bounded_collect(None, {}, rows, 10, .01)
        self.assertEqual(len(rows), 1)
        self.assertIn('лимит времени', reason)

    async def test_flood_retains_rows(self):
        rows = {1: Member('A', '', 1, 'member')}
        with patch('collector.collect', AsyncMock(side_effect=errors.FloodWaitError(None, 42))):
            reason = await bounded_collect(None, {}, rows, 10, 1)
        self.assertIn('42', reason)
        self.assertEqual(len(rows), 1)

    async def test_revoked_before_send_no_files(self):
        api = NS(send=AsyncMock(), document=AsyncMock(), call=AsyncMock(side_effect=[10, []]))
        app = App(api, None, 99)
        app.rights = AsyncMock(side_effect=[{'id': -1001, 'type': 'channel'}, ValueError('Права отозваны')])
        with patch('bot.bounded_collect', AsyncMock(return_value='Частично')):
            await app.export(1, -1001)
        api.document.assert_not_awaited()
        self.assertNotIn(1, app.results)

    async def test_cancel_removes_job(self):
        api = NS(send=AsyncMock())
        app = App(api, None, 99)
        async def waiting(*args):
            await asyncio.sleep(5)
        app.rights = waiting
        task = asyncio.create_task(app.export(1, -1001))
        app.jobs[1] = task
        await asyncio.sleep(.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(1, app.jobs)


if __name__ == '__main__':
    unittest.main()
