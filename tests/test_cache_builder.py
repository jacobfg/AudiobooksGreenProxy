import json
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from utils.cache_builder import CacheBuilder


class CacheBuilderTests(unittest.TestCase):
    def setUp(self):
        self.builder = CacheBuilder()

    def test_source_file_validation_accepts_single_m4b_and_multiple_mp3s(self):
        m4b = {"ino": "one", "metadata": {"filename": "Book.m4b"}}
        mp3_a = {"ino": "one", "metadata": {"filename": "01.mp3"}}
        mp3_b = {"ino": "two", "metadata": {"filename": "02.mp3"}}

        self.assertEqual(self.builder._source_audio_files({"media": {"audioFiles": [m4b]}}), [m4b])
        self.assertEqual(
            self.builder._source_audio_files({"media": {"audioFiles": [mp3_a, mp3_b]}}),
            [mp3_a, mp3_b],
        )

    def test_multiple_non_mp3_sources_are_rejected(self):
        item = {
            "media": {
                "audioFiles": [
                    {"ino": "one", "metadata": {"filename": "01.m4b"}},
                    {"ino": "two", "metadata": {"filename": "02.m4b"}},
                ]
            }
        }
        with self.assertRaisesRegex(RuntimeError, "Multiple source files"):
            self.builder._source_audio_files(item)

    def test_chapters_split_at_thirty_minutes(self):
        with patch("utils.cache_builder.subprocess.run") as run:
            run.return_value = Mock(
                returncode=0,
                stdout=json.dumps(
                    {
                        "chapters": [
                            {
                                "start_time": "0.0",
                                "end_time": "1900.0",
                                "tags": {"title": "Long chapter"},
                            }
                        ]
                    }
                ),
                stderr="",
            )
            chapter = self.builder._chapters(Path("book.m4b"))[0]

        parts = list(self.builder._split_unit(chapter[0], chapter[1]))
        self.assertEqual(parts, [(0.0, 1800.0), (1800.0, 100.0)])

    def test_garmin_filename_is_ascii_and_safe(self):
        self.assertEqual(
            self.builder._clean_name('Chapter: “A/B” & Café'),
            "Chapter A B Cafe",
        )


if __name__ == "__main__":
    unittest.main()
