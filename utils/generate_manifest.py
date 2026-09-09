#!/usr/bin/env python3
"""Generate a proxy cache manifest from one book's rendered audio directory.

Run this on the machine that creates the cache. It requires ffprobe (from
FFmpeg) so the manifest has correct playback durations.
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


AUDIO_EXTENSIONS = {".mp3", ".m4a", ".m4b", ".wav", ".adts"}


def natural_sort_key(path):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", path.as_posix())
    ]


def probe_duration(path, ffprobe):
    command = [
        ffprobe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("ffprobe returned no duration") from exc
    if duration < 0:
        raise RuntimeError("ffprobe returned a negative duration")
    return duration


def build_manifest(book_directory, ffprobe="ffprobe"):
    book_directory = Path(book_directory).resolve()
    files = sorted(
        (
            path
            for path in book_directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in AUDIO_EXTENSIONS
        ),
        key=lambda path: natural_sort_key(path.relative_to(book_directory)),
    )
    entries = []
    for index, path in enumerate(files, start=1):
        relative_path = path.relative_to(book_directory)
        entries.append(
            {
                "id": f"chunk-{index:04d}",
                "path": relative_path.as_posix(),
                "filename": path.name,
                "duration": probe_duration(path, ffprobe),
            }
        )
    return {"files": entries}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "book_directory",
        type=Path,
        help="Cache directory for one ABS item, e.g. CACHE_DIR/<item-uuid>",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Manifest output path (default: <book_directory>/manifest.json)",
    )
    parser.add_argument(
        "--ffprobe",
        default="ffprobe",
        help="ffprobe executable to use (default: ffprobe)",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace an existing manifest.json",
    )
    args = parser.parse_args()

    book_directory = args.book_directory.resolve()
    if not book_directory.is_dir():
        parser.error(f"Book directory does not exist: {book_directory}")
    if shutil.which(args.ffprobe) is None:
        parser.error(f"ffprobe executable not found: {args.ffprobe}")

    output = (args.output or book_directory / "manifest.json").resolve()
    if output.exists() and not args.overwrite:
        parser.error(f"Manifest already exists: {output} (use --overwrite to replace it)")

    try:
        manifest = build_manifest(book_directory, args.ffprobe)
    except RuntimeError as exc:
        print(f"Unable to generate manifest: {exc}", file=sys.stderr)
        return 1

    output.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(manifest['files'])} files to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
