import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from audiobookshelf import redused_api as abs_api


class MediaCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cache_root = Path(self.temp_dir.name)
        self.environment = patch.dict(os.environ, {"CACHE_DIR": str(self.cache_root)})
        self.environment.start()
        self.book_dir = self.cache_root / "book-1"
        self.book_dir.mkdir()

    def tearDown(self):
        self.environment.stop()
        self.temp_dir.cleanup()

    def write_manifest(self, files):
        (self.book_dir / "manifest.json").write_text(json.dumps({"files": files}))

    def test_valid_manifest_returns_exact_watch_file_fields(self):
        (self.book_dir / "chapter-001.mp3").write_bytes(b"audio")
        self.write_manifest(
            [
                {
                    "id": "chunk_1",
                    "path": "chapter-001.mp3",
                    "filename": "Chapter 1.mp3",
                    "duration": 12.5,
                }
            ]
        )

        files = abs_api.get_cached_files("book-1")

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["id"], "chunk_1")
        self.assertEqual(files[0]["filename"], "Chapter 1.mp3")
        self.assertEqual(files[0]["duration"], 12.5)
        self.assertEqual(files[0]["path"], self.book_dir / "chapter-001.mp3")

    def test_missing_manifest_or_referenced_file_hides_entire_book(self):
        self.assertIsNone(abs_api.get_cached_files("book-1"))

        self.write_manifest(
            [
                {
                    "id": "chunk-1",
                    "path": "missing.mp3",
                    "filename": "Missing.mp3",
                    "duration": 1,
                }
            ]
        )
        self.assertIsNone(abs_api.get_cached_files("book-1"))

    def test_path_traversal_and_duplicate_ids_invalidate_manifest(self):
        (self.book_dir / "chapter.mp3").write_bytes(b"audio")
        self.write_manifest(
            [
                {
                    "id": "chunk-1",
                    "path": "../chapter.mp3",
                    "filename": "Chapter.mp3",
                    "duration": 1,
                }
            ]
        )
        self.assertIsNone(abs_api.get_cached_files("book-1"))

        self.write_manifest(
            [
                {
                    "id": "chunk-1",
                    "path": "chapter.mp3",
                    "filename": "Chapter.mp3",
                    "duration": 1,
                },
                {
                    "id": "chunk-1",
                    "path": "chapter.mp3",
                    "filename": "Again.mp3",
                    "duration": 1,
                },
            ]
        )
        self.assertIsNone(abs_api.get_cached_files("book-1"))

    def test_download_resolves_manifest_id_and_rejects_unknown_id(self):
        cached_file = self.book_dir / "chapter.mp3"
        cached_file.write_bytes(b"audio")
        self.write_manifest(
            [
                {
                    "id": "chunk-1",
                    "path": "chapter.mp3",
                    "filename": "Chapter.mp3",
                    "duration": 1,
                }
            ]
        )

        response = abs_api.get_cached_media_response("book-1", "chunk-1")
        self.assertEqual(Path(response.path), cached_file)
        with self.assertRaises(HTTPException) as error:
            abs_api.get_cached_media_response("book-1", "missing")
        self.assertEqual(error.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
