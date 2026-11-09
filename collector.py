import asyncio
from telethon import functions, types, errors
from export_core import Member


def member(user, participant):
    kind = type(participant).__name__
    role = 'owner' if 'Creator' in kind else 'admin' if 'Admin' in kind else 'member'
    return Member(' '.join(x for x in (user.first_name, user.last_name) if x),
                  user.username or '', user.id, role,
                  getattr(participant, 'rank', None) or getattr(participant, 'title', None) or '')


async def collect(client, chat, rows, max_members, on_progress=None):
    """Mutates rows so timeout/FloodWait retains already received pages."""
    if chat['type'] == 'group':
        result = await client(functions.messages.GetFullChatRequest(chat['id'] * -1))
        participants = getattr(result.full_chat.participants, 'participants', None)
        if participants is None:
            return 'Telegram не предоставил список участников обычной группы.'
        users = {u.id: u for u in result.users}
        for p in participants:
            if p.user_id in users:
                rows[p.user_id] = member(users[p.user_id], p)
        return 'Telegram вернул список обычной группы; полнота снимка не гарантируется.'
    # Bots may use access_hash=0 for channels they belong to. Public usernames
    # are resolved normally; private chats use this bot-specific peer form.
    if chat.get('username'):
        peer = await client.get_input_entity('@' + chat['username'])
    else:
        peer = types.InputChannel(int(str(chat['id'])[4:]), 0)
    offset = 0
    while len(rows) < max_members:
        page = await client(functions.channels.GetParticipantsRequest(
            peer, types.ChannelParticipantsRecent(), offset, min(200, max_members - len(rows)), 0))
        users = {u.id: u for u in page.users}
        new = 0
        for p in page.participants:
            user = users.get(getattr(p, 'user_id', None))
            if user and user.id not in rows:
                rows[user.id] = member(user, p)
                new += 1
        if not page.participants:
            return 'Telegram завершил выдачу доступного списка; скрытые участники могут отсутствовать.'
        if new == 0:
            return 'Telegram повторил страницу без новых участников; выдача остановлена.'
        offset += len(page.participants)
        if on_progress:
            await on_progress(len(rows))
        await asyncio.sleep(.35)
    return 'Достигнут установленный лимит количества участников.'


async def bounded_collect(client, chat, rows, max_members, seconds, on_progress=None, on_flood=None):
    try:
        async with asyncio.timeout(seconds):
            return await collect(client, chat, rows, max_members, on_progress)
    except TimeoutError:
        return f'Достигнут лимит времени {seconds} секунд; результат частичный.'
    except errors.FloodWaitError as e:
        if on_flood:
            on_flood(e.seconds)
        return f'Telegram требует паузу {e.seconds} секунд (FloodWait); результат частичный. Повторите позже.'
    except errors.RPCError as e:
        return f'Telegram ограничил выдачу ({type(e).__name__}); результат частичный.'
