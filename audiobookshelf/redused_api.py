import uuid
from math import e
import io
import aiohttp
import tempfile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi import HTTPException
from pydantic import BaseModel
from PIL import Image
import os


class AudiobookshelfProgressItem(BaseModel):
    libraryItemId: str
    currentTime: float
    progress: float


class AudiobookshelfProgress(BaseModel):
    server: str
    token: str
    items: list[AudiobookshelfProgressItem]


class AudiobookshelfSessionItem(BaseModel):
    libraryItemId: str
    # The watch works in whole seconds; Monkey C's Number is 32-bit, so a
    # millisecond epoch cannot be represented there. Conversion happens here.
    updatedAt: int
    currentTime: float
    duration: float = 0.0
    timeListening: int = 0
    # Stable per listening stretch. Reused across retries so the server
    # upserts one row instead of double-counting stats.
    sessionKey: str
    displayTitle: str = ""
    displayAuthor: str = ""


class AudiobookshelfSessions(BaseModel):
    server: str
    token: str
    items: list[AudiobookshelfSessionItem]


class AudiobookshelfAithorizationData(BaseModel):
    server: str
    login: str
    password: str


def sanitze_server_name(server):
    override = os.environ.get("SERVER_ENDPOINT")
    if override:
        return override.rstrip("/")
        
    result = server
    if not result.startswith("https://"):
        result = "https://" + result
    if result.endswith("/"):
        result = result[:-1]
    return result


def clear_token(token):
    return token.replace("Bearer ", "").strip()


def get_book_info(resp_book):
    # Validate id presence; only missing id is considered an error
    book_id = None
    if isinstance(resp_book, dict):
        book_id = resp_book.get("id")
    if not book_id:
        raise HTTPException(status_code=500, detail="Missing 'id' in book data")

    # Safely extract nested fields with defaults
    media = resp_book.get("media") if isinstance(resp_book, dict) else None
    metadata = media.get("metadata") if isinstance(media, dict) else None

    title = ""
    if isinstance(metadata, dict):
        title = metadata.get("title", "") or ""

    author = ""
    if isinstance(metadata, dict):
        authors = metadata.get("authors")
        if (
            isinstance(authors, list)
            and len(authors) > 0
            and isinstance(authors[0], dict)
        ):
            author = authors[0].get("name", "") or ""

    res = {
        "id": book_id,
        "author": author,
        "title": title,
        # "cover": media.get("coverPath") if isinstance(media, dict) else "",
    }

    return res


async def get_playlists(server, token):
    result = []
    url = f"{sanitze_server_name(server)}/api/playlists"
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.ok:
                    resp_json = await resp.json()
                    for resp_playlist in resp_json["playlists"]:
                        result.append(
                            {
                                "id": resp_playlist["id"],
                                "libraryId": resp_playlist["libraryId"],
                                "name": resp_playlist["name"],
                            }
                        )
                else:
                    resp_content_b = await resp.content.read()
                    raise HTTPException(
                        status_code=resp.status,
                        detail=resp_content_b.decode("utf-8"),
                    )
        except aiohttp.ClientConnectorError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Network connection error to Audiobookshelf server: {e}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"An unexpected error occurred while fetching playlists: {e}",
            )

    return result


async def get_playlist(server, playlist_id, token):
    result = []
    url = f"{sanitze_server_name(server)}/api/playlists/{playlist_id}"
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.ok:
                    resp_json = await resp.json()
                    for resp_book in resp_json["items"]:
                        result.append(get_book_info(resp_book["libraryItem"]))
                else:
                    resp_content_b = await resp.content.read()
                    raise HTTPException(
                        status_code=resp.status,
                        detail=resp_content_b.decode("utf-8"),
                    )
        except aiohttp.ClientConnectorError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Network connection error to Audiobookshelf server: {e}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"An unexpected error occurred while fetching playlist: {e}",
            )

    return result


async def get_book(server, book_id, token, skip=0, limit=0):
    result = {}
    url = f"{sanitze_server_name(server)}/api/items/{book_id}"
    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.ok:
                    resp_book = await resp.json()
                    result = get_book_info(resp_book)

                    files_list_on_server = resp_book["media"]["audioFiles"]
                    result["total"] = len(files_list_on_server)
                    result["skip"] = skip
                    result["limit"] = limit
                    files = []
                    counter = 0
                    added = 0
                    for file in files_list_on_server:
                        if counter >= skip and (
                            limit == 0 or (limit > 0 and added < limit)
                        ):
                            added += 1
                            files.append(
                                {
                                    "filename": file["metadata"]["filename"],
                                    "duration": file["duration"],
                                    "id": file["ino"],
                                }
                            )
                        counter += 1
                    result["files"] = files
                else:
                    resp_content_b = await resp.content.read()
                    raise HTTPException(
                        status_code=resp.status,
                        detail=resp_content_b.decode("utf-8"),
                    )
        except aiohttp.ClientConnectorError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Network connection error to Audiobookshelf server: {e}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"An unexpected error occurred while fetching book details: {e}",
            )

    return result


async def login(server, login, password):
    result = {}
    url = f"{sanitze_server_name(server)}/login"
    json_data = {"username": login, "password": password}
    headers = {"Content-Type": "application/json", "x-return-tokens": "true"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=json_data) as resp:
                if resp.ok:
                    resp_json = await resp.json()
                    keys_list = ["token", "refreshToken", "accessToken"]
                     # With the x-return-tokens header, Audiobookshelf returns
                     # tokens at the top level under "tokens" (not under "user").
                     # Support both layouts so we read whichever is present.
                    tokens = resp_json.get("tokens")
                    user = resp_json.get("user") if isinstance(resp_json, dict) else None
                    if isinstance(tokens, dict):
                        for key_name in keys_list:
                            if key_name in tokens:
                                result[key_name] = tokens[key_name]
                    if isinstance(user, dict):
                        for key_name in keys_list:
                            if key_name in user and key_name not in result:
                                result[key_name] = user[key_name]
                else:
                    resp_content_b = await resp.content.read()
                    raise HTTPException(
                        status_code=resp.status,
                        detail=resp_content_b.decode("utf-8"),
                    )
        except aiohttp.ClientConnectorError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Network connection error to Audiobookshelf server: {e}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"An unexpected error occurred while getting token: {e}",
            )

    return result


async def get_cover(url):
    if ("api/items" in url and "cover" in url) or ("preview" in url):
        async with aiohttp.ClientSession() as session:
            try:
                async with session.get(url) as resp:
                    if resp.ok:
                        data = await resp.read()
                        content_type = resp.headers.get("Content-Type", "image/jpeg")

                        # Если это не JPEG, конвертируем в JPEG
                        if content_type != "image/jpeg":
                            try:
                                # Открываем картинку из bytes
                                image = Image.open(io.BytesIO(data))

                                # Конвертируем в RGB если нужно (для картинок с альфа-каналом)
                                if image.mode in ("RGBA", "LA", "P"):
                                    rgb_image = Image.new(
                                        "RGB", image.size, (255, 255, 255)
                                    )
                                    if image.mode in ("RGBA", "LA"):
                                        rgb_image.paste(image, mask=image.split()[-1])
                                    else:
                                        rgb_image.paste(image)
                                    image = rgb_image
                                elif image.mode != "RGB":
                                    image = image.convert("RGB")

                                # Сохраняем в JPEG формат
                                output = io.BytesIO()
                                image.save(output, format="JPEG", quality=85)
                                output.seek(0)
                                data = output.getvalue()
                                content_type = "image/jpeg"
                            except Exception as e:
                                raise HTTPException(
                                    status_code=500,
                                    detail=f"Failed to convert image to JPEG: {e}",
                                )

                        return StreamingResponse(
                            io.BytesIO(data),
                            media_type=content_type,
                            headers={
                                "Content-Length": str(len(data)),
                                "Content-Disposition": "attachment; filename=cover.jpg",
                            },
                        )
                    else:
                        resp_content_b = await resp.content.read()
                        raise HTTPException(
                            status_code=resp.status,
                            detail=f"Failed to fetch cover image: {resp_content_b.decode('utf-8')}",
                        )
            except aiohttp.ClientConnectorError as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"Network connection error while fetching cover: {e}",
                )
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(
                    status_code=500,
                    detail=f"An unexpected error occurred while fetching cover: {e}",
                )

    # URL did not match a known cover/preview pattern: return an explicit 204
    # instead of leaking a null body the client may misread.
    return FileResponse(
        os.devnull,
        media_type="image/jpeg",
        status_code=204,
     )


async def sync_sessions(data):
    url = sanitze_server_name(data.server) + "/api/session/local-all"
    headers = {
        "Authorization": f"Bearer {data.token}",
        "Content-Type": "application/json",
    }

    sessions = []
    for item in data.items:
        # The server compares updatedAt against a JS Date .valueOf(), i.e. a
        # millisecond epoch. Passing seconds parses as 1970, loses every
        # comparison, and the sync is dropped with HTTP 200.
        updated_at_ms = item.updatedAt * 1000
        progress = 0.0
        if item.duration > 0:
            progress = min(item.currentTime / item.duration, 1.0)

        sessions.append(
            {
                "id": str(
                    uuid.uuid5(
                        uuid.NAMESPACE_URL,
                        f"abgreen:{item.libraryItemId}:{item.sessionKey}",
                    )
                ),
                "libraryItemId": item.libraryItemId,
                "mediaType": "book",
                "currentTime": item.currentTime,
                "timeListening": item.timeListening,
                "duration": item.duration,
                "progress": progress,
                "startedAt": updated_at_ms - (item.timeListening * 1000),
                "updatedAt": updated_at_ms,
                "displayTitle": item.displayTitle,
                "displayAuthor": item.displayAuthor,
                "mediaPlayer": "garmin-audiobooks-green",
                "playMethod": 3,
            }
        )

    req_data = {
        "sessions": sessions,
        "deviceInfo": {
            "clientName": "AudiobooksGreen",
            "deviceType": "watch",
        },
    }

    async with aiohttp.ClientSession() as session:
        try:
            async with session.post(url, headers=headers, json=req_data) as resp:
                if resp.ok:
                    return await resp.json()
                else:
                    resp_content_b = await resp.content.read()
                    raise HTTPException(
                        status_code=resp.status,
                        detail=resp_content_b.decode("utf-8"),
                    )
        except aiohttp.ClientConnectorError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Network connection error to Audiobookshelf server: {e}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"An unexpected error occurred while syncing sessions: {e}",
            )


async def set_progress(data):
    url = sanitze_server_name(data.server)
    url += "/api/me/progress/batch/update"
    headers = {
        "Authorization": f"Bearer {data.token}",
        "Content-Type": "application/json",
    }
    req_data = []
    for item in data.items:
        req_data.append(
            {
                "libraryItemId": item.libraryItemId,
                "currentTime": item.currentTime,
                "progress": item.progress,
            }
        )
    async with aiohttp.ClientSession() as session:
        try:
            async with session.patch(url, headers=headers, json=req_data) as resp:
                if resp.ok:
                    return await resp.text()
                else:
                    resp_content_b = await resp.content.read()
                    raise HTTPException(
                        status_code=resp.status,
                        detail=resp_content_b.decode("utf-8"),
                    )
        except aiohttp.ClientConnectorError as e:
            raise HTTPException(
                status_code=500,
                detail=f"Network connection error to Audiobookshelf server: {e}",
            )
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(
                status_code=500,
                detail=f"An unexpected error occurred while setting progress: {e}",
            )
