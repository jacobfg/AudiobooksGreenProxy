import os
import json
import asyncio
import aiohttp
from pydantic import BaseModel
from typing import Optional
import base64
from fastapi import Response


class TelegramMessage(BaseModel):
    # token/chat_id are kept in the payload for backward compatibility with the
    # Garmin IQ app, but are NO LONGER trusted for the actual Bot API call.
    # The bot token (which grants full control of the bot) must never be trusted
    # from the client: when the server is configured it always wins.
    token: str = ""
    chat_id: Optional[int] = None
    message: str = ""


def _configured_token() -> str:
    return (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()


def _configured_chat_id() -> Optional[int]:
    # Optional server-side default destination. When unset we fall back to the
    # caller-provided chat_id (a chat id is not a secret).
    raw = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    if raw.lstrip("-").isdigit():
        try:
            return int(raw)
        except ValueError:
            return None
    return None


def telegram_is_configured() -> bool:
    # The outbound forward is only enabled when the server owns a bot token.
    return bool(_configured_token())


# Shared session reused across requests instead of opening a new connection
# pool per call. Created lazily inside the running event loop.
_session: Optional[aiohttp.ClientSession] = None
_session_lock = asyncio.Lock()


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        # Serialize lazy creation so concurrent requests don't each build one.
        async with _session_lock:
            if _session is None or _session.closed:
                _session = aiohttp.ClientSession()
    return _session


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Разбивает текст на части, не превышая max_length и не разрывая строки."""
    # Если текст пустой, возвращаем пустой список
    if not text:
        return []

    lines = text.splitlines(keepends=True)
    chunks = []
    current_chunk = ""

    for line in lines:
        # Если одна строка длиннее лимита, жестко режем ее
        if len(line) > max_length:
            if current_chunk:
                chunks.append(current_chunk)
                current_chunk = ""

            # Режем длинную строку на куски
            long_line = line
            while len(long_line) > max_length:
                chunks.append(long_line[:max_length])
                long_line = long_line[max_length:]
            current_chunk = long_line
            continue

        # Если добавление строки превысит лимит, сохраняем текущий кусок
        if len(current_chunk) + len(line) > max_length:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk += line

    # Добавляем последний оставшийся кусок
    if current_chunk:
        chunks.append(current_chunk)

    return chunks


async def forward_message_to_telegram(data: TelegramMessage):

    # Graceful no-op when Telegram is not configured (i.e. the Telegram env
    # vars are not set). The Garmin IQ app still gets a Telegram-shaped
    # success response, but nothing is sent and no outbound call is made.
    if not telegram_is_configured():
        return Response(
            content=json.dumps(
                {"ok": True, "skipped": True, "description": "telegram not configured"}
            ),
            media_type="application/json",
        )

    # Security: the server-owned token always wins. The client-supplied token
    # is ignored so a caller cannot relay/abuse an arbitrary bot token.
    token = _configured_token()
    chat_id = _configured_chat_id()
    if chat_id is None:
        chat_id = data.chat_id
    if chat_id is None:
        return Response(
            content=json.dumps(
                {"ok": False, "error": "no chat_id configured or provided"}
            ),
            media_type="application/json",
            status_code=400,
        )

    try:
        decoded_bytes = base64.b64decode(data.message, validate=True)
        final_message = decoded_bytes.decode("utf-8")
    except Exception:
        final_message = data.message

    # Разбиваем сообщение на части с учетом лимита Telegram
    message_chunks = split_message(final_message, max_length=4000)

    url = f"https://api.telegram.org/bot{token}/sendMessage"

    proxy_host = os.getenv("TELEGRAM_PROXY_HOST")
    proxy_port = os.getenv("TELEGRAM_PROXY_PORT")
    proxy_url = (
        f"http://{proxy_host}:{proxy_port}" if proxy_host and proxy_port else None
    )

    last_response_text = ""

    session = await _get_session()
    for chunk in message_chunks:
        payload = {"chat_id": chat_id, "text": chunk}

        async with session.post(url, data=payload, proxy=proxy_url) as response:
            response.raise_for_status()
            last_response_text = await response.text()

    # Возвращаем результат последней отправки для совместимости с FastAPI Response
    return Response(content=last_response_text, media_type="application/json")
