import asyncio
import httpx


class ApiError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f'Telegram API: {code}')


class BotAPI:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.client = httpx.AsyncClient(timeout=45)

    async def call(self, method, **data):
        for attempt in range(3):
            try:
                response = await self.client.post(self.base + method, json=data)
                result = response.json()
            except (httpx.HTTPError, ValueError):
                raise ApiError('network') from None
            if result.get('ok'):
                return result['result']
            if result.get('error_code') == 429:
                delay = result.get('parameters', {}).get('retry_after', 1)
                if delay <= 30 and attempt < 2:
                    await asyncio.sleep(delay + 1)
                    continue
            raise ApiError(result.get('error_code', 'unknown'))
        raise ApiError(429)

    async def send(self, uid, text, **extra):
        return await self.call('sendMessage', chat_id=uid,
                               text=text + '\n\nСоздатель — @auedurov', **extra)

    async def document(self, uid, path):
        try:
            with path.open('rb') as file:
                response = await self.client.post(self.base + 'sendDocument',
                    data={'chat_id': str(uid), 'protect_content': 'true'},
                    files={'document': (path.name, file)}, timeout=90)
            result = response.json()
            if not result.get('ok'):
                raise ApiError(result.get('error_code', 'unknown'))
        except (httpx.HTTPError, ValueError):
            raise ApiError('network') from None
