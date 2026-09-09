import json
import logging
import mimetypes
import re
import uuid
import io
import aiohttp
import tempfile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi import HTTPException
from pydantic import BaseModel
from PIL import Image
import os
from pathlib import Path


logger = logging.getLogger(__name__)

CACHE_FILE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


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
    return (token or "").replace("Bearer ", "").strip()


def _cache_root():
    return Path(os.environ.get("CACHE_DIR", "/app/cache")).resolve()


def _book_cache_directory(book_id):
    """Return the cache directory for a book, rejecting path traversal."""
    book_dir = (_cache_root() / book_id).resolve()
    try:
        book_dir.relative_to(_cache_root())
    except ValueError:
        logger.warning("Rejected unsafe cache book id")
        return None
    return book_dir


def get_cached_files(book_id):
    """Read and fully validate a book's cache manifest.

    The cache is deliberately all-or-nothing. A partially populated rendition
    must not be presented to the watch as a playable audiobook.
    """
    book_dir = _book_cache_directory(book_id)
    if book_dir is None:
        return None

    manifest_path = book_dir / "manifest.json"
    try:
        with manifest_path.open(encoding="utf-8") as manifest_file:
            manifest = json.load(manifest_file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("Cache unavailable for book %s: %s", book_id, exc)
        return None

    if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list):
        logger.warning("Cache manifest for book %s has no files list", book_id)
        return None

    files = []
    ids = set()
    for entry in manifest["files"]:
        if not isinstance(entry, dict):
            logger.warning("Cache manifest for book %s has an invalid entry", book_id)
            return None

        file_id = entry.get("id")
        relative_path = entry.get("path")
        filename = entry.get("filename")
        duration = entry.get("duration")
        if (
            not isinstance(file_id, str)
            or not CACHE_FILE_ID_RE.fullmatch(file_id)
            or file_id in ids
            or not isinstance(relative_path, str)
            or not relative_path
            or not isinstance(filename, str)
            or not filename
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or duration < 0
        ):
            logger.warning("Cache manifest for book %s has invalid file metadata", book_id)
            return None

        resolved_path = (book_dir / relative_path).resolve()
        try:
            resolved_path.relative_to(book_dir)
        except ValueError:
            logger.warning("Cache manifest for book %s contains an unsafe path", book_id)
            return None

        if not resolved_path.is_file():
            logger.info("Cache unavailable for book %s: a listed file is missing", book_id)
            return None

        ids.add(file_id)
        files.append(
            {
                "id": file_id,
                "path": resolved_path,
                "filename": filename,
                "duration": float(duration),
            }
        )
    return files


def get_cached_file(book_id, file_id):
    for entry in get_cached_files(book_id) or []:
        if entry["id"] == file_id:
            return entry
    return None


def get_server_endpoint():
    """The real ABS endpoint for direct watch-facing proxy routes."""
    endpoint = os.environ.get("SERVER_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise HTTPException(status_code=503, detail="SERVER_ENDPOINT is not configured")
    return endpoint


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
                    # The watch must see the locally cached rendition, not
                    # necessarily ABS's original (for example, one M4B may be
                    # represented by many compatible MP3 chapters).
                    cached_files = get_cached_files(book_id) or []
                    result["total"] = len(cached_files)
                    result["skip"] = skip
                    result["limit"] = limit
                    selected_files = cached_files[skip:]
                    if limit > 0:
                        selected_files = selected_files[:limit]
                    result["files"] = [
                        {
                            "filename": file["filename"],
                            "duration": file["duration"],
                            "id": file["id"],
                        }
                        for file in selected_files
                    ]
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


async def get_progress(book_id, token):
    """Forward the small progress response used directly by the watch app."""
    url = f"{get_server_endpoint()}/api/me/progress/{book_id}"
    headers = {"Authorization": f"Bearer {clear_token(token)}"}
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers) as resp:
                if resp.ok:
                    return await resp.json()
                response_text = await resp.text()
                raise HTTPException(status_code=resp.status, detail=response_text)
        except aiohttp.ClientConnectorError as exc:
            raise HTTPException(status_code=502, detail="Unable to reach Audiobookshelf") from exc


async def get_book_cover(book_id, token, params):
    """Forward an image request while preserving ABS's image response."""
    url = f"{get_server_endpoint()}/api/items/{book_id}/cover"
    headers = {}
    if clear_token(token):
        headers["Authorization"] = f"Bearer {clear_token(token)}"
    async with aiohttp.ClientSession() as session:
        try:
            async with session.get(url, headers=headers, params=params) as resp:
                if not resp.ok:
                    response_text = await resp.text()
                    raise HTTPException(status_code=resp.status, detail=response_text)
                data = await resp.read()
                return StreamingResponse(
                    io.BytesIO(data),
                    media_type=resp.headers.get("Content-Type", "image/jpeg"),
                    headers={"Content-Length": str(len(data))},
                )
        except aiohttp.ClientConnectorError as exc:
            raise HTTPException(status_code=502, detail="Unable to reach Audiobookshelf") from exc


def get_cached_media_response(book_id, file_id):
    entry = get_cached_file(book_id, file_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Cached media file not found")
    media_type = mimetypes.guess_type(entry["filename"])[0] or "application/octet-stream"
    return FileResponse(
        entry["path"], media_type=media_type, filename=entry["filename"]
    )


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
