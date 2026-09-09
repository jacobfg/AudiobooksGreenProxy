"""Background builder for Garmin-compatible Audiobookshelf media caches."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import unicodedata
import uuid
from pathlib import Path

import aiohttp

from audiobookshelf import redused_api as abs_api
from utils.generate_manifest import build_manifest


logger = logging.getLogger(__name__)
SAFE_ITEM_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SUPPORTED_SOURCE_EXTENSIONS = {".m4b", ".mp3"}


class CacheBuilder:
    """Queue missing playlist books for one-at-a-time cache construction."""

    def __init__(self):
        self.pending = set()
        self.semaphore = asyncio.Semaphore(
            max(1, int(os.getenv("AUTO_CACHE_MAX_JOBS", "1")))
        )

    @property
    def enabled(self):
        return os.getenv("AUTO_CACHE_ENABLED", "false").lower() in {
            "1",
            "true",
            "yes",
        }

    def queue_missing_books(self, server, token, books):
        """Start background builds without delaying the Garmin playlist reply."""
        if not self.enabled:
            return
        for book in books:
            book_id = book.get("id")
            if (
                not isinstance(book_id, str)
                or not SAFE_ITEM_ID.fullmatch(book_id)
                or abs_api.get_cached_files(book_id) is not None
                or book_id in self.pending
            ):
                continue
            self.pending.add(book_id)
            asyncio.create_task(self._build_and_forget(server, token, book))
            logger.info("Queued Garmin cache build for book %s", book_id)

    async def _build_and_forget(self, server, token, book):
        book_id = book["id"]
        try:
            async with self.semaphore:
                await self._build(server, token, book)
        except Exception:
            logger.exception("Garmin cache build failed for book %s", book_id)
        finally:
            self.pending.discard(book_id)

    async def _build(self, server, token, book):
        book_id = book["id"]
        cache_root = Path(os.getenv("CACHE_DIR", "/app/cache")).resolve()
        staging_root = cache_root / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        stage = staging_root / f"{book_id}-{uuid.uuid4().hex}"
        stage.mkdir()

        try:
            item = await self._get_item(server, token, book_id)
            audio_files = self._source_audio_files(item)
            source_dir = stage / "sources"
            source_dir.mkdir()
            downloaded = await self._download_sources(
                server, token, book_id, audio_files, source_dir
            )
            output_dir = await asyncio.to_thread(
                self._render_garmin_media,
                stage,
                downloaded,
                audio_files,
                book.get("title", book_id),
            )
            manifest = await asyncio.to_thread(build_manifest, output_dir)
            (output_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
            )
            self._validate_output(output_dir, manifest)
            await asyncio.to_thread(self._publish, cache_root, book_id, output_dir, stage)
            logger.info("Garmin cache build complete for book %s", book_id)
        except Exception:
            # Preserve failed source/intermediate files for diagnosis. Successful
            # builds remove the entire staging tree after publication.
            logger.error("Garmin cache staging retained at %s", stage)
            raise

    async def _get_item(self, server, token, book_id):
        url = f"{abs_api.sanitze_server_name(server)}/api/items/{book_id}"
        headers = {"Authorization": f"Bearer {abs_api.clear_token(token)}"}
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as response:
                if not response.ok:
                    raise RuntimeError(
                        f"ABS item request failed ({response.status}): {await response.text()}"
                    )
                return await response.json()

    def _source_audio_files(self, item):
        media = item.get("media") if isinstance(item, dict) else None
        files = media.get("audioFiles") if isinstance(media, dict) else None
        if not isinstance(files, list) or not files:
            raise RuntimeError("ABS item has no audio files")
        result = []
        for file_info in files:
            metadata = file_info.get("metadata") if isinstance(file_info, dict) else None
            filename = metadata.get("filename") if isinstance(metadata, dict) else None
            file_id = file_info.get("ino") if isinstance(file_info, dict) else None
            extension = Path(filename or "").suffix.casefold()
            if not filename or file_id is None or extension not in SUPPORTED_SOURCE_EXTENSIONS:
                raise RuntimeError("Only M4B and MP3 ABS source files are supported")
            result.append(file_info)
        if len(result) > 1 and any(
            Path(file["metadata"]["filename"]).suffix.casefold() != ".mp3"
            for file in result
        ):
            raise RuntimeError("Multiple source files must all be MP3")
        return result

    async def _download_sources(self, server, token, book_id, audio_files, source_dir):
        base_url = abs_api.sanitze_server_name(server)
        headers = {"Authorization": f"Bearer {abs_api.clear_token(token)}"}
        downloaded = []
        async with aiohttp.ClientSession() as session:
            for index, file_info in enumerate(audio_files, start=1):
                filename = file_info["metadata"]["filename"]
                extension = Path(filename).suffix.casefold()
                target = source_dir / f"source-{index:04d}{extension}"
                url = f"{base_url}/api/items/{book_id}/file/{file_info['ino']}/download"
                async with session.get(url, headers=headers) as response:
                    if not response.ok:
                        raise RuntimeError(
                            f"ABS media download failed ({response.status}): {await response.text()}"
                        )
                    with target.open("wb") as output:
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            output.write(chunk)
                downloaded.append(target)
        return downloaded

    def _render_garmin_media(self, stage, downloaded, audio_files, book_title):
        """Render M4B chapters or source-MP3 boundaries to Garmin MP3 files."""
        output_dir = stage / "rendered"
        output_dir.mkdir()
        index = 1
        for source_path, source_info in zip(downloaded, audio_files):
            units = self._chapters(source_path)
            if len(downloaded) > 1:
                units = [(0.0, self._probe_duration(source_path), Path(source_info["metadata"]["filename"]).stem)]
            for start, duration, title in units:
                for part_start, part_duration in self._split_unit(start, duration):
                    filename = self._next_output_filename(output_dir, index, title)
                    self._run(
                        [
                            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                            "-ss", f"{part_start:.3f}", "-i", str(source_path),
                            "-t", f"{part_duration:.3f}", "-vn", "-map_metadata", "-1",
                            "-map_chapters", "-1", "-ar", "44100", "-ac", "2",
                            "-c:a", "libmp3lame", "-b:a", "96k",
                            "-metadata", f"title={filename.stem}",
                            "-metadata", f"album={self._clean_name(book_title)}",
                            "-metadata", "genre=Audiobook", "-metadata", f"track={index}",
                            str(filename),
                        ],
                        stage,
                    )
                    index += 1
        if index == 1:
            raise RuntimeError("No Garmin media was rendered")
        return output_dir

    def _chapters(self, source_path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-of", "json", str(source_path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {source_path.name}")
        chapters = json.loads(result.stdout).get("chapters", [])
        units = []
        for chapter in chapters:
            try:
                start = float(chapter["start_time"])
                end = float(chapter["end_time"])
            except (KeyError, TypeError, ValueError):
                continue
            if end > start:
                units.append((start, end - start, chapter.get("tags", {}).get("title") or "Chapter"))
        return units or [(0.0, self._probe_duration(source_path), Path(source_path).stem)]

    @staticmethod
    def _split_unit(start, duration):
        max_seconds = int(os.getenv("AUTO_CACHE_CHAPTER_MINUTES", "30")) * 60
        current = start
        end = start + duration
        while current < end:
            next_position = min(current + max_seconds, end)
            yield current, next_position - current
            current = next_position

    def _next_output_filename(self, output_dir, index, title):
        safe_title = self._clean_name(title) or f"Chapter {index:03d}"
        candidate = output_dir / f"{index:03d} - {safe_title}.mp3"
        duplicate = 1
        while candidate.exists():
            candidate = output_dir / f"{index:03d} - {safe_title} ({duplicate}).mp3"
            duplicate += 1
        return candidate

    @staticmethod
    def _clean_name(value):
        value = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
        value = re.sub(r"[/:*?<>|\\\\]", " ", value)
        value = re.sub(r"[^A-Za-z0-9._ -]+", "", value)
        return re.sub(r"\s+", " ", value).strip(". ")

    @staticmethod
    def _probe_duration(path):
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or f"ffprobe failed for {path.name}")
        return float(result.stdout.strip())

    @staticmethod
    def _run(command, cwd):
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "Command failed: " + " ".join(command))

    @staticmethod
    def _validate_output(output_dir, manifest):
        files = manifest.get("files") if isinstance(manifest, dict) else None
        if not isinstance(files, list) or not files:
            raise RuntimeError("Converter produced no manifest files")
        for entry in files:
            if not (output_dir / entry["path"]).is_file():
                raise RuntimeError("Generated manifest references a missing file")

    @staticmethod
    def _publish(cache_root, book_id, output_dir, stage):
        target = (cache_root / book_id).resolve()
        target.relative_to(cache_root)
        prepared = stage / "published"
        shutil.move(str(output_dir), prepared)
        backup = None
        try:
            if target.exists():
                backup = stage / "previous-cache"
                target.rename(backup)
            prepared.rename(target)
        except Exception:
            if backup is not None and backup.exists() and not target.exists():
                backup.rename(target)
            raise
        finally:
            shutil.rmtree(stage, ignore_errors=True)
