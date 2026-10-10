from __future__ import annotations
import aiohttp
import asyncio
import logging
from typing import Any

log = logging.getLogger(__name__)

class BaleAPIError(RuntimeError):
    pass

class BaleAPI:
    def __init__(self, token: str, timeout: int = 40):
        self.base = f'https://tapi.bale.ai/bot{token}/'
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self.session: aiohttp.ClientSession | None = None

    async def start(self) -> None:
        self.session = aiohttp.ClientSession(timeout=self.timeout)

    async def close(self) -> None:
        if self.session:
            await self.session.close()
            self.session = None

    async def call(self, method: str, payload: dict[str, Any] | None = None, *, retries: int = 2) -> Any:
        if not self.session:
            raise RuntimeError('Bale API session is not started')
        last_error = None
        for attempt in range(retries + 1):
            try:
                async with self.session.post(self.base + method, json=payload or {}) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 500 and attempt < retries:
                        await asyncio.sleep(1 + attempt)
                        continue
                    if response.status >= 400 or not data.get('ok', response.status < 400):
                        raise BaleAPIError(f'{method}: HTTP {response.status}: {data.get("description", data)}')
                    return data.get('result', data)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_error = exc
                if attempt < retries:
                    await asyncio.sleep(1 + attempt)
                    continue
                break
        raise BaleAPIError(f'{method}: {last_error}')

    async def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        payload: dict[str, Any] = {'timeout': timeout, 'allowed_updates': ['message', 'callback_query', 'my_chat_member', 'chat_member']}
        if offset is not None:
            payload['offset'] = offset
        result = await self.call('getUpdates', payload, retries=0)
        return result if isinstance(result, list) else []

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None, reply_to_message_id: int | None = None, parse_mode: str | None = 'Markdown') -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'text': text}
        if reply_markup is not None: payload['reply_markup'] = reply_markup
        if reply_to_message_id is not None: payload['reply_to_message_id'] = reply_to_message_id
        if parse_mode: payload['parse_mode'] = parse_mode
        return await self.call('sendMessage', payload)

    async def edit_message(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None, parse_mode: str | None = 'Markdown') -> dict:
        payload: dict[str, Any] = {'chat_id': chat_id, 'message_id': message_id, 'text': text}
        if reply_markup is not None: payload['reply_markup'] = reply_markup
        if parse_mode: payload['parse_mode'] = parse_mode
        return await self.call('editMessageText', payload)

    async def answer_callback(self, callback_id: str, text: str = '') -> None:
        payload: dict[str, Any] = {'callback_query_id': callback_id}
        if text: payload['text'] = text
        await self.call('answerCallbackQuery', payload)

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        await self.call('deleteMessage', {'chat_id': chat_id, 'message_id': message_id})

    async def pin_message(self, chat_id: int, message_id: int) -> None:
        await self.call('pinChatMessage', {'chat_id': chat_id, 'message_id': message_id, 'disable_notification': True})

    async def get_chat(self, chat_id: int) -> dict:
        return await self.call('getChat', {'chat_id': chat_id})
