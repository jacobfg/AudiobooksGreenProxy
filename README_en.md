[Русский](/README.md) 

# AudiobooksGreenProxy

This is a self-hosted proxy to work in conjunction with the audiobook player [AudiobooksGreen](https://github.com/fabrikant/AudiobooksGreen).

## Serving watch-compatible cached media

The proxy can be the only public URL configured in AudiobooksGreen. Set both
the app's **Server** and **Proxy** URLs to the proxy's public HTTPS URL, enable
**prefer proxy requests**, and set `SERVER_ENDPOINT` on the proxy to the real,
private Audiobookshelf URL. The proxy then reads book metadata and progress
from Audiobookshelf but serves watch media from a local cache.

Set `CACHE_DIR` (default `/app/cache`) to a writable cache root. Each cached
book must use its ABS library-item ID and contain a manifest:

```text
CACHE_DIR/
  <abs-book-id>/
    manifest.json
    chapter-001.mp3
    chapter-002.mp3
```

```json
{
  "files": [
    {
      "id": "a1",
      "path": "chapter-001.mp3",
      "filename": "Chapter 1.mp3",
      "duration": 123.45
    }
  ]
}
```

`id` must be unique within the manifest and contain only letters, digits,
underscores, or hyphens. `path` must be a relative path inside the book cache
directory. The proxy validates every entry before listing a book; if the
manifest is missing or invalid, or any listed file is missing, the watch sees
an empty file list. This prevents partial cached audiobooks from being offered.

### Automatic cache building

Set `AUTO_CACHE_ENABLED=true` to have the proxy queue missing or invalid
playlist books for background conversion whenever the watch requests a
playlist. It downloads source M4B or MP3 media from ABS, reads embedded M4B
chapters (or uses MP3-file boundaries), and renders 96 kb/s stereo MP3 chunks.
Chunks are limited to 30 minutes by `AUTO_CACHE_CHAPTER_MINUTES`.

The first watch sync after adding a book can show no files while it is being
built. Run Full sync again after conversion finishes. The proxy keeps complete
results in the cache and preserves failed job workspaces under `.staging/` for
diagnosis.

### Generating a manifest

`utils/generate_manifest.py` creates a manifest for one book cache directory.
It recursively finds supported audio files, orders them naturally (so `2.mp3`
comes before `10.mp3`), and gets durations with `ffprobe` from FFmpeg:

```sh
python3 utils/generate_manifest.py ./media-cache/<abs-book-id>
```

It refuses to overwrite a manifest. Regenerate deliberately with:

```sh
python3 utils/generate_manifest.py --overwrite ./media-cache/<abs-book-id>
```

# Installation and Launch Methods

## Docker compose - from a pre-built image (recommended)
Create a directory on your server. In it, create a **docker-compose.yml** file with content from the [docker-compose.yml.example](https://github.com/fabrikant/AudiobooksGreenProxy/blob/main/docker-compose.yml.example) file. Following the recommendations in the comments, set your own values.

Next to the **docker-compose.yml** file, create a **data** directory.

### Launch
```
docker compose up -d
```

### Stop
```
docker compose down
```

### Update
```
docker compose pull
docker compose down
docker compose up -d
docker image prune
```

# Building a Docker image from source

```
docker build -t audiobooks_green_proxy .
```

Launch it the same way as in the previous section. Only in the **docker-compose.yml** file, you need to replace the image name with the one you have in your system.
You can view image names using the command:
```
docker image ls -a
```

# Running Python Code

### Virtual environment setup and configuration
```
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install --upgrade -r requirements.txt
mkdir data
deactivate
```

### Launch
Activate the virtual environment
```
source .venv/bin/activate
```

Set environment variables (if required)
```
export SSL_CERT_FILE=./data/fullchain.pem
export SSL_PRIVATE_KEY_FILE=./data/privkey.pem
```

Launch proxy
```
python3 books_proxy.py
```
