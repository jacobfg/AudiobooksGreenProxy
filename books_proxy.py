import asyncio
import uvicorn
from fastapi import FastAPI, Header, Query
from typing import Optional
import os
import logging
from telethon import TelegramClient, events
import tg_bot.tg_bot_debugger as tg_bot
from utils.telegram_forwarder import TelegramMessage, forward_message_to_telegram

logger = logging.getLogger(__name__)

try:
    TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", ""))
except ValueError:
    logger.info("TELEGRAM_API_ID environment variable is not a valid integer.")
    TELEGRAM_API_ID = 0  # Or handle the error as appropriate for your application
except TypeError:
    logger.info(
        "TELEGRAM_API_ID environment variable is not set or is not a valid integer."
    )
    TELEGRAM_API_ID = 0  # Or handle the error as appropriate for your application

TG_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")


# Проверка, должен ли Telegram-бот быть запущен
# Считаем, что бот готов к старту, только если все три переменные корректны.
# TELEGRAM_API_ID не может быть 0 для рабочего бота, HASH и TOKEN не должны быть пустыми.
start_tg_bot = bool(TELEGRAM_API_ID != 0 and TG_API_HASH and TELEGRAM_BOT_TOKEN)
if start_tg_bot:

    proxy = (
        {
            "proxy_type": "http",
            "addr": os.getenv("TELEGRAM_PROXY_HOST"),
            "port": int(os.getenv("TELEGRAM_PROXY_PORT")),
        }
        if os.getenv("TELEGRAM_PROXY_HOST") and os.getenv("TELEGRAM_PROXY_PORT")
        else None
    )

    client = TelegramClient(
        "./data/tg-bot.session",
        TELEGRAM_API_ID,
        TG_API_HASH,
        proxy=proxy,
    )
else:
    client = tg_bot.FakeClient()


show_swagger_str = os.getenv("SHOW_SWAGGER", default="0")
if show_swagger_str.lower() in ["true", "1", "yes"]:
    app = FastAPI()
else:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

###############################################################################
#
#               FAST API
#
###############################################################################


# =============================================================================
# Audiobookshelf

import audiobookshelf.redused_api as abs


# *****************************************************************************
@app.post("/audiobookshelf/login")
async def audiobookshelf_login(data: abs.AudiobookshelfAithorizationData):
    return await abs.login(data.server, data.login, data.password)


# *****************************************************************************
@app.get("/audiobookshelf/playlists")
async def proxy_audiobookshelf_playlists(
    server: str = Query(
        "", alias="server", description="URL your Audiobookshelf server."
    ),
    token: Optional[str] = Header(
        None,
        alias="Authorization",
        description="Header Authorization: Bearer your_token_here",
    ),
):
    return await abs.get_playlists(server, abs.clear_token(token))


# *****************************************************************************
@app.get("/audiobookshelf/playlist")
async def audiobookshelf_playlist(
    server: str,
    playlist_id: str,
    token: Optional[str] = Header(
        None,
        alias="Authorization",
        description="Header Authorization: Bearer your_token_here",
    ),
):
    return await abs.get_playlist(server, playlist_id, abs.clear_token(token))


# *****************************************************************************
@app.get("/audiobookshelf/book")
async def audiobookshelf_book(
    server: str,
    book_id: str,
    skip: int = 0,
    limit: int = 0,
    token: Optional[str] = Header(
        None,
        alias="Authorization",
        description="Header Authorization: Bearer your_token_here",
    ),
):
    return await abs.get_book(server, book_id, abs.clear_token(token), skip, limit)


# *****************************************************************************
@app.get("/audiobookshelf/cover")
async def audiobookshelf_cover(url: str):
    return await abs.get_cover(url)


@app.post("/audiobookshelf/set_progress")
async def set_progress(data: abs.AudiobookshelfProgress):
    return await abs.set_progress(data)


# *****************************************************************************
@app.post("/send_telegram_message")
async def akniga_set_progress(
    data: TelegramMessage,
):
    return await forward_message_to_telegram(data)


# *****************************************************************************
@app.post("/audiobookshelf/sync_sessions")
async def sync_sessions(data: abs.AudiobookshelfSessions):
    return await abs.sync_sessions(data)


# *****************************************************************************
@app.get("/")
async def root():
    return {"message": "OK"}


###############################################################################
#
#               TELEGRAM BOT
#
###############################################################################


@client.on(events.NewMessage(pattern="/start"))
async def get_my_id(event):
    await tg_bot.get_my_id(client, event)


###############################################################################
#
#               STARTER
#
###############################################################################


async def main(uvicorn_log_level):

    global start_tg_bot

    loop = asyncio.get_event_loop()

    # FAST API
    SSL_CERT_FILE = os.getenv("SSL_CERT_FILE", None)
    if SSL_CERT_FILE:
        if not os.path.exists(SSL_CERT_FILE):
            SSL_CERT_FILE = None

    SSL_PRIVATE_KEY_FILE = os.getenv("SSL_PRIVATE_KEY_FILE", None)
    if SSL_PRIVATE_KEY_FILE:
        if not os.path.exists(SSL_PRIVATE_KEY_FILE):
            SSL_PRIVATE_KEY_FILE = None

    config = uvicorn.Config(
        "books_proxy:app",
        host="0.0.0.0",
        port=8081,
        log_level=uvicorn_log_level,
        access_log=True,
        reload=False,
        ssl_keyfile=SSL_PRIVATE_KEY_FILE,
        ssl_certfile=SSL_CERT_FILE,
    )
    server = uvicorn.Server(config)
    fastapi_task = loop.create_task(server.serve())
    logger.info("Main: FastAPI server task created.")

    # TELEGRAM
    telethon_task = None
    if start_tg_bot:
        logger.info("Main: Attempting to start Telethon client...")
        try:
            await client.start(bot_token=TELEGRAM_BOT_TOKEN)
            await tg_bot.set_bot_commands(client)
            logger.info("Main: Telethon client connected.")
            telethon_task = loop.create_task(client.run_until_disconnected())
            logger.info("Main: Telethon client listener task created.")
        except Exception as e:
            logger.error(f"Main: Failed to start Telethon client: {e}", exc_info=True)
            start_tg_bot = False  # Mark as failed to start even if configured
    else:
        logger.info(
            "Main: Telethon client will not be started as start_tg_bot is False."
        )

    try:
        logger.info("Main: Running event loop forever. Press Ctrl+C to stop.")
        tasks_to_gather = [fastapi_task]
        if telethon_task:
            tasks_to_gather.append(telethon_task)

        await asyncio.gather(*tasks_to_gather)

    except KeyboardInterrupt:
        logger.warning("Main: KeyboardInterrupt detected. Shutting down...")
    finally:
        if start_tg_bot:  # Отключаемся только если клиент был успешно запущен
            logger.info("Main: Disconnecting Telethon client...")
            await client.disconnect()
            logger.info("Main: Telethon client disconnected.")
        logger.info("Main: Event loop closing.")


if __name__ == "__main__":
    uvicorn_log_level = os.getenv("LOG_LEVEL", default="info")

    logger_level = logging.INFO
    if uvicorn_log_level.lower().strip() in ["critical", "error"]:
        logger_level = logging.ERROR
    elif uvicorn_log_level.lower().strip() == "warning":
        logger_level = logging.WARNING
    elif uvicorn_log_level.lower().strip() == "info":
        logger_level = logging.INFO
    elif uvicorn_log_level.lower().strip() in ["debug", "trace"]:
        logger_level = logging.DEBUG

    logging.basicConfig(
        level=logger_level,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    asyncio.run(main(uvicorn_log_level))
