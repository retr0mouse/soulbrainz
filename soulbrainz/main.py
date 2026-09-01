#!/usr/bin/env python3

import logging
import os
import re
import shutil
import time
import uuid
from pathlib import Path

import requests
from mutagen import File as MutagenFile

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

LISTENBRAINZ_URL = "https://api.listenbrainz.org/1"
LISTENBRAINZ_USER = os.environ.get("LISTENBRAINZ_USER", "reisdro")

SLSKD_URL = os.environ.get(
    "SLSKD_URL",
    "https://slskd.voldsoy.duckdns.org",
)

SLSKD_API_KEY = os.environ.get("SLSKD_API_KEY")

MUSIC_DIR = Path("/data/music")
DOWNLOAD_DIR = Path("/data/downloads/slskd/complete")

SEARCH_TIMEOUT = 60
DOWNLOAD_TIMEOUT = 300

RECOMMENDATION_COUNT = 5

AUDIO_EXTENSIONS = {
    ".flac",
    ".mp3",
    ".ogg",
    ".opus",
    ".m4a",
    ".aac",
    ".wav",
    ".alac",
}

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("soulbrainz")


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _slskd_headers():
    headers = {
        "Accept": "application/json",
    }

    if SLSKD_API_KEY:
        headers["X-API-Key"] = SLSKD_API_KEY

    return headers


# ---------------------------------------------------------------------------
# ListenBrainz
# ---------------------------------------------------------------------------

def get_weekly_jams():
    """
    Get the user's current Weekly Jams playlist.

    Returns:
        list[dict]: [{"artist": "...", "title": "..."}]
    """

    url = (
        f"{LISTENBRAINZ_URL}/user/"
        f"{LISTENBRAINZ_USER}/playlists/createdfor"
    )

    resp = requests.get(url, timeout=20)
    resp.raise_for_status()

    data = resp.json()

    playlists = data.get("playlists", [])

    weekly_jams = None

    for playlist in playlists:
        title = playlist.get("title", "").lower()

        if title == "weekly jams":
            weekly_jams = playlist
            break

    if not weekly_jams:
        raise RuntimeError("Could not find Weekly Jams playlist")

    playlist_mbid = weekly_jams.get("playlist_mbid")

    if not playlist_mbid:
        raise RuntimeError("Weekly Jams playlist has no playlist_mbid")

    log.info(
        "Found Weekly Jams playlist: %s",
        playlist_mbid,
    )

    playlist_url = f"{LISTENBRAINZ_URL}/playlist/{playlist_mbid}"

    resp = requests.get(playlist_url, timeout=20)
    resp.raise_for_status()

    data = resp.json()

    playlist = data.get("playlist", {})
    tracks = playlist.get("track", [])

    result = []

    for track in tracks:
        artist = track.get("creator")
        title = track.get("trackName")

        if not artist or not title:
            continue

        result.append({
            "artist": artist.strip(),
            "title": title.strip(),
        })

    log.info("Found %d tracks", len(result))

    return result[:RECOMMENDATION_COUNT]


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------

def _normalise_metadata(value):
    """
    Normalize metadata sufficiently for duplicate detection.
    """

    if not value:
        return ""

    value = str(value).strip().lower()

    # Normalize Unicode-ish punctuation commonly encountered in tags.
    value = value.replace("’", "'")
    value = value.replace("‘", "'")
    value = value.replace("–", "-")
    value = value.replace("—", "-")

    # Collapse whitespace.
    value = re.sub(r"\s+", " ", value)

    return value


def _first_tag(audio, *keys):
    """
    Get the first useful value from a set of possible Mutagen tag names.
    """

    if not audio or not audio.tags:
        return None

    for key in keys:
        value = audio.tags.get(key)

        if value is None:
            continue

        if isinstance(value, list):
            if not value:
                continue
            value = value[0]

        return str(value)

    return None


def read_audio_metadata(path):
    """
    Return normalized artist/title metadata from an audio file.

    Returns:
        tuple[str, str] | None
    """

    try:
        audio = MutagenFile(path, easy=True)

        if audio is None:
            return None

        artist = _first_tag(
            audio,
            "artist",
            "albumartist",
        )

        title = _first_tag(
            audio,
            "title",
        )

        if not artist or not title:
            return None

        return (
            _normalise_metadata(artist),
            _normalise_metadata(title),
        )

    except Exception as exc:
        log.debug(
            "Could not read metadata from %s: %s",
            path,
            exc,
        )

        return None


def build_library_index():
    """
    Scan /data/music and build a set of (artist, title) pairs.

    Metadata is used instead of filenames because the existing library
    contains several different filename conventions.
    """

    log.info("Scanning music library: %s", MUSIC_DIR)

    index = set()

    if not MUSIC_DIR.exists():
        log.warning("Music directory does not exist: %s", MUSIC_DIR)
        return index

    file_count = 0

    for path in MUSIC_DIR.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        file_count += 1

        metadata = read_audio_metadata(path)

        if metadata:
            index.add(metadata)

    log.info(
        "Indexed %d audio files, %d unique artist/title pairs",
        file_count,
        len(index),
    )

    return index


def track_exists(library_index, artist, title):
    key = (
        _normalise_metadata(artist),
        _normalise_metadata(title),
    )

    return key in library_index


# ---------------------------------------------------------------------------
# slskd search
# ---------------------------------------------------------------------------

def _ext(filename):
    """
    Return a lowercase extension including the dot.
    """

    return Path(filename).suffix.lower()


def search_slskd(artist, title):
    """
    Submit a search to slskd and return individual file results.
    """

    search_id = str(uuid.uuid4())
    query = f"{artist} {title}".strip()

    log.info("Searching Soulseek: %s", query)

    resp = requests.post(
        f"{SLSKD_URL}/api/v0/searches",
        json={
            "id": search_id,
            "searchText": query,
        },
        headers=_slskd_headers(),
        timeout=10,
    )

    resp.raise_for_status()

    deadline = time.monotonic() + SEARCH_TIMEOUT

    while time.monotonic() < deadline:
        time.sleep(2)

        resp = requests.get(
            f"{SLSKD_URL}/api/v0/searches/{search_id}",
            headers=_slskd_headers(),
            timeout=10,
        )

        resp.raise_for_status()

        data = resp.json()

        if data.get("isComplete"):
            log.info(
                "Search completed: %s — %d files from %d users",
                data.get("state"),
                data.get("fileCount", 0),
                data.get("responseCount", 0),
            )

            resp = requests.get(
                f"{SLSKD_URL}/api/v0/searches/{search_id}/responses",
                headers=_slskd_headers(),
                timeout=10,
            )

            resp.raise_for_status()

            responses = resp.json()

            files = []

            for response in responses:
                username = response.get("username")

                for file in response.get("files", []):
                    file["_username"] = username
                    files.append(file)

            log.info(
                "Retrieved %d files from %d users",
                len(files),
                len(responses),
            )

            return files

    log.warning(
        "Search timed out for: %s",
        query,
    )

    return []


# ---------------------------------------------------------------------------
# Search result selection
# ---------------------------------------------------------------------------

def _rank(file):
    """
    Lower is better.

    Priority:
        0 = FLAC
        1 = MP3 >= 320 kbps
        2 = MP3 < 320 kbps
        3 = other supported audio
    """

    ext = _ext(file["filename"])

    if ext == ".flac":
        return 0

    if ext == ".mp3":
        bitrate = file.get("bitRate", 0) or 0

        if bitrate >= 320:
            return 1

        return 2

    if ext in AUDIO_EXTENSIONS:
        return 3

    return 99


def pick_best(files):
    """
    Pick the best usable audio file from Soulseek results.
    """

    audio = [
        file
        for file in files
        if _ext(file.get("filename", "")) in AUDIO_EXTENSIONS
           and not file.get("isLocked", False)
    ]

    if not audio:
        return None

    return sorted(audio, key=_rank)[0]


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------

def sanitize_filename(value):
    """
    Make a string safe to use as a Unix filename.
    """

    value = value.strip()

    # Replace characters that are problematic or commonly illegal
    # in cross-platform music filenames.
    value = re.sub(r"[\/\\:*?\"<>|]", "_", value)

    # Avoid accidental whitespace at the end.
    value = value.rstrip(" .")

    if not value:
        return "Unknown"

    return value


def extract_track_number(filename):
    """
    Try to extract an album track number from a Soulseek filename.

    Examples:

        01 Boys Don't Cry.flac          -> 01
        Foo Fighters - Everlong - 01    -> 01
        7 - Everlong.mp3                -> 07

    Returns:
        str | None
    """

    name = Path(filename).stem

    patterns = [
        r"^\s*(\d{1,2})[\s._-]+",
        r"[\s._-]+(\d{1,2})[\s._-]+[^0-9]*$",
        r"[\s._-]+(\d{1,2})[\s._-]+",
    ]

    for pattern in patterns:
        match = re.search(pattern, name)

        if match:
            number = int(match.group(1))

            if 1 <= number <= 99:
                return f"{number:02d}"

    return None


def build_destination(artist, title, source_filename):
    """
    Build:

        /data/music/<Artist>/<NN>. <Title>.<ext>
    """

    artist_dir = MUSIC_DIR / sanitize_filename(artist)

    extension = _ext(source_filename)

    track_number = extract_track_number(source_filename)

    safe_title = sanitize_filename(title)

    if track_number:
        filename = f"{track_number}. {safe_title}{extension}"
    else:
        filename = f"{safe_title}{extension}"

    return artist_dir / filename


# ---------------------------------------------------------------------------
# Download handling
# ---------------------------------------------------------------------------

def snapshot_download_files():
    """
    Take a snapshot of files currently in the slskd completed directory.

    slskd may preserve the remote directory hierarchy, so we search
    recursively later.
    """

    if not DOWNLOAD_DIR.exists():
        return set()

    return {
        path
        for path in DOWNLOAD_DIR.rglob("*")
        if path.is_file()
    }


def queue_download(file):
    """
    Queue one Soulseek file through slskd.

    The API expects a LIST of QueueDownloadRequest objects.
    """

    username = file.get("_username")
    filename = file.get("filename")
    size = file.get("size")

    if not username:
        raise RuntimeError("Search result has no username")

    if not filename:
        raise RuntimeError("Search result has no filename")

    log.info(
        "Queuing download: %s from %s",
        filename,
        username,
    )

    resp = requests.post(
        f"{SLSKD_URL}/api/v0/transfers/downloads/{username}",
        json=[
            {
                "filename": filename,
                "size": size,
            }
        ],
        headers=_slskd_headers(),
        timeout=10,
    )

    if not resp.ok:
        log.warning(
            "Download request failed: %s — %s",
            resp.status_code,
            resp.text,
        )

    resp.raise_for_status()


def _filename_matches(path, remote_filename):
    """
    Compare a completed local file against the requested remote filename.

    slskd may transform path separators but should preserve the basename.
    """

    return path.name == Path(remote_filename.replace("\\", "/")).name


def wait_for_download(
        file,
        previous_files,
        timeout=DOWNLOAD_TIMEOUT,
):
    """
    Wait until the requested file appears in the completed download
    directory.

    We intentionally use the filesystem rather than relying on slskd's
    transfer-state JSON, because the latter varies between slskd versions
    and configurations.
    """

    remote_filename = file["filename"]
    expected_size = file.get("size")

    deadline = time.monotonic() + timeout

    log.info(
        "Waiting for download: %s",
        remote_filename,
    )

    while time.monotonic() < deadline:
        if not DOWNLOAD_DIR.exists():
            time.sleep(2)
            continue

        candidates = []

        for path in DOWNLOAD_DIR.rglob("*"):
            if not path.is_file():
                continue

            if path in previous_files:
                continue

            if not _filename_matches(path, remote_filename):
                continue

            candidates.append(path)

        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                continue

            # If slskd gave us a size, use it as an additional guard.
            if expected_size and size != expected_size:
                continue

            log.info(
                "Download completed: %s",
                path,
            )

            return path

        time.sleep(2)

    raise TimeoutError(
        f"Timed out waiting for download: {remote_filename}"
    )


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def import_download(
        source,
        artist,
        title,
):
    """
    Move the completed slskd download into the normalized music library.
    """

    destination = build_destination(
        artist,
        title,
        source.name,
    )

    destination.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    if destination.exists():
        log.warning(
            "Destination already exists, skipping import: %s",
            destination,
        )
        return False

    log.info(
        "Importing: %s -> %s",
        source,
        destination,
    )

    shutil.move(
        str(source),
        str(destination),
    )

    # Try to remove empty directories left behind by slskd.
    cleanup_empty_parents(source.parent)

    return True


def cleanup_empty_parents(directory):
    """
    Remove empty directories beneath DOWNLOAD_DIR.
    """

    directory = Path(directory)

    while directory != DOWNLOAD_DIR and DOWNLOAD_DIR in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            break

        directory = directory.parent


# ---------------------------------------------------------------------------
# Track processing
# ---------------------------------------------------------------------------

def process_track(track, library_index):
    artist = track["artist"]
    title = track["title"]

    log.info(
        "Processing: %s — %s",
        artist,
        title,
    )

    # -----------------------------------------------------------------------
    # 1. Check the actual music library first.
    # -----------------------------------------------------------------------

    if track_exists(
            library_index,
            artist,
            title,
    ):
        log.info(
            "Already in library, skipping: %s — %s",
            artist,
            title,
        )
        return False

    # -----------------------------------------------------------------------
    # 2. Search Soulseek.
    # -----------------------------------------------------------------------

    files = search_slskd(
        artist,
        title,
    )

    if not files:
        log.warning(
            "No Soulseek results: %s — %s",
            artist,
            title,
        )
        return False

    # -----------------------------------------------------------------------
    # 3. Select the best result.
    # -----------------------------------------------------------------------

    selected = pick_best(files)

    if not selected:
        log.warning(
            "No usable audio result: %s — %s",
            artist,
            title,
        )
        return False

    log.info(
        "Selected: %s from %s",
        selected["filename"],
        selected.get("_username"),
    )

    extension = _ext(selected["filename"])

    if extension == ".flac":
        log.info("Selected quality: FLAC")

    elif extension == ".mp3":
        log.info(
            "Selected quality: MP3 %s kbps",
            selected.get("bitRate", "?"),
        )

    else:
        log.info(
            "Selected format: %s",
            extension,
        )

    # -----------------------------------------------------------------------
    # 4. Snapshot completed downloads before queueing.
    # -----------------------------------------------------------------------

    previous_files = snapshot_download_files()

    # -----------------------------------------------------------------------
    # 5. Queue download.
    # -----------------------------------------------------------------------

    queue_download(selected)

    # -----------------------------------------------------------------------
    # 6. Wait for the actual file to appear.
    # -----------------------------------------------------------------------

    try:
        completed_file = wait_for_download(
            selected,
            previous_files,
        )

    except TimeoutError as exc:
        log.error("%s", exc)
        return False

    # -----------------------------------------------------------------------
    # 7. Move it into /data/music/<Artist>/...
    # -----------------------------------------------------------------------

    imported = import_download(
        completed_file,
        artist,
        title,
    )

    if imported:
        # Update the in-memory index so that another Weekly Jams entry
        # cannot cause a duplicate during this same execution.
        library_index.add(
            (
                _normalise_metadata(artist),
                _normalise_metadata(title),
            )
        )

    return imported


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("Starting soulbrainz")

    MUSIC_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    DOWNLOAD_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # -----------------------------------------------------------------------
    # Build the library index once.
    # -----------------------------------------------------------------------

    library_index = build_library_index()

    # -----------------------------------------------------------------------
    # Fetch Weekly Jams.
    # -----------------------------------------------------------------------

    tracks = get_weekly_jams()

    if not tracks:
        log.info("No tracks to process")
        return

    downloaded = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------------------------
    # Process each recommendation.
    # -----------------------------------------------------------------------

    for track in tracks:
        try:
            result = process_track(
                track,
                library_index,
            )

            if result:
                downloaded += 1
            else:
                skipped += 1

        except Exception:
            failed += 1

            log.exception(
                "Failed processing %s — %s",
                track.get("artist"),
                track.get("title"),
            )

    # -----------------------------------------------------------------------
    # Summary.
    # -----------------------------------------------------------------------

    log.info(
        "Finished: %d imported, %d skipped, %d failed",
        downloaded,
        skipped,
        failed,
    )


if __name__ == "__main__":
    main()
