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

    resp = requests.get(
        url,
        timeout=20,
    )
    resp.raise_for_status()

    data = resp.json()

    playlists = data.get("playlists", [])

    weekly_jams = None

    for playlist_entry in playlists:
        playlist = playlist_entry["playlist"]
        title = playlist["title"]

        if f"Weekly Jams for {LISTENBRAINZ_USER}" in title:
            weekly_jams = playlist
            break

    if not weekly_jams:
        raise RuntimeError(
            "Could not find Weekly Jams playlist"
        )

    mbid = (
        weekly_jams["identifier"]
        .rstrip("/")
        .split("/")[-1]
    )

    if not mbid:
        raise RuntimeError(
            "Weekly Jams playlist has no mbid"
        )

    log.info(
        "Found Weekly Jams playlist: %s",
        mbid,
    )

    playlist_url = (
        f"{LISTENBRAINZ_URL}/playlist/{mbid}"
    )

    resp = requests.get(
        playlist_url,
        timeout=20,
    )
    resp.raise_for_status()

    data = resp.json()

    playlist = data.get("playlist", {})
    tracks = playlist.get("track", [])

    result = []

    for track in tracks:
        artist = track.get("creator")
        title = track.get("title")

        if not artist or not title:
            continue

        result.append(
            {
                "artist": artist.strip(),
                "title": title.strip(),
            }
        )

    log.info(
        "Found %d tracks",
        len(result),
    )

    return result


# ---------------------------------------------------------------------------
# Metadata normalization
# ---------------------------------------------------------------------------

def _normalise_metadata(value):
    """
    Normalize metadata for duplicate and track matching.

    Examples:

        System of a Down
        System Of A Down
        System-of-a-Down

    all become comparable.
    """

    if not value:
        return ""

    value = str(value).strip().lower()

    value = value.replace("&", " and ")

    # Normalize common Unicode punctuation.
    value = value.replace("’", "'")
    value = value.replace("‘", "'")
    value = value.replace("–", "-")
    value = value.replace("—", "-")
    value = value.replace("‐", "-")
    value = value.replace("-", "-")

    # Remove punctuation.
    value = re.sub(
        r"[^\w\s]",
        " ",
        value,
        flags=re.UNICODE,
    )

    # Collapse whitespace.
    value = re.sub(
        r"\s+",
        " ",
        value,
    )

    return value.strip()


def _normalise_track_title(value):
    """
    Normalize a track title.

    Featuring information is not stripped here because it can legitimately
    be part of a title. Artist feature handling belongs in artist matching.
    """

    return _normalise_metadata(value)


def _normalise_artist_for_matching(value):
    """
    Normalize an artist name for matching.

    Common featured-artist suffixes are removed.

    Examples:

        Kid Cudi
        Kid Cudi feat. CeeLo Green
        Kid Cudi ft CeeLo Green
        Kid Cudi featuring CeeLo Green

    all normalize to:

        kid cudi
    """

    value = _normalise_metadata(value)

    # Remove everything starting with a common featuring separator.
    value = re.split(
        r"\b(?:feat|ft|featuring)\b",
        value,
        maxsplit=1,
    )[0]

    return value.strip()


def _artist_matches(expected, actual):
    """
    Compare artist names while allowing common featured-artist notation.
    """

    expected_normalized = _normalise_artist_for_matching(
        expected
    )

    actual_normalized = _normalise_artist_for_matching(
        actual
    )

    if not expected_normalized or not actual_normalized:
        return False

    return (
        expected_normalized == actual_normalized
    )


# ---------------------------------------------------------------------------
# Audio metadata
# ---------------------------------------------------------------------------

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
    Return raw artist/title metadata from an audio file.

    Returns:
        tuple[str, str] | None
    """

    try:
        audio = MutagenFile(
            path,
            easy=True,
        )

        if audio is None:
            return None

        artist = _first_tag(
            audio,
            "artist",
        )

        if not artist:
            artist = _first_tag(
                audio,
                "albumartist",
            )

        title = _first_tag(
            audio,
            "title",
        )

        if not artist or not title:
            return None

        return (
            artist,
            title,
        )

    except Exception as exc:
        log.debug(
            "Could not read metadata from %s: %s",
            path,
            exc,
        )

        return None


# ---------------------------------------------------------------------------
# Music library
# ---------------------------------------------------------------------------

def _extract_library_title(filename):
    """
    Extract a track title from the standardized library filename.

    Examples:

        01. Toxicity.flac       -> Toxicity
        07 - Everlong.mp3       -> Everlong
        03_some song.flac       -> some song
    """

    name = Path(filename).stem

    name = re.sub(
        r"^\s*\d{1,2}[\s._-]+",
        "",
        name,
    )

    return name.strip()


def build_library_index():
    """
    Scan /data/music and build a set of (artist, title) pairs.

    Metadata is preferred. The standardized directory/filename layout is
    also indexed as a fallback.
    """

    log.info(
        "Scanning music library: %s",
        MUSIC_DIR,
    )

    index = set()

    if not MUSIC_DIR.exists():
        log.warning(
            "Music directory does not exist: %s",
            MUSIC_DIR,
        )

        return index

    file_count = 0
    metadata_count = 0
    path_fallback_count = 0

    for path in MUSIC_DIR.rglob("*"):
        if not path.is_file():
            continue

        if path.suffix.lower() not in AUDIO_EXTENSIONS:
            continue

        file_count += 1

        metadata = read_audio_metadata(path)

        if metadata:
            artist, title = metadata

            index.add(
                (
                    _normalise_artist_for_matching(artist),
                    _normalise_track_title(title),
                )
            )

            metadata_count += 1

        if path.parent != MUSIC_DIR:
            artist = path.parent.name
            title = _extract_library_title(path.name)

            if artist and title:
                index.add(
                    (
                        _normalise_artist_for_matching(artist),
                        _normalise_track_title(title),
                    )
                )

                path_fallback_count += 1

    log.info(
        "Indexed %d audio files, %d unique artist/title pairs "
        "(%d metadata, %d path fallback)",
        file_count,
        len(index),
        metadata_count,
        path_fallback_count,
    )

    return index


def track_exists(library_index, artist, title):
    key = (
        _normalise_artist_for_matching(artist),
        _normalise_track_title(title),
    )

    return key in library_index


# ---------------------------------------------------------------------------
# slskd search
# ---------------------------------------------------------------------------

def _ext(filename):
    return Path(filename).suffix.lower()


def search_slskd(artist, title):
    """
    Submit a search to slskd and return individual file results.
    """

    search_id = str(uuid.uuid4())
    query = f"{artist} {title}".strip()

    log.info(
        "Searching Soulseek: %s",
        query,
    )

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

    deadline = (
        time.monotonic()
        + SEARCH_TIMEOUT
    )

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
                f"{SLSKD_URL}/api/v0/searches/"
                f"{search_id}/responses",
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
# Soulseek result matching
# ---------------------------------------------------------------------------

def _strip_track_number(value):
    """
    Remove a leading track number.

    Examples:

        01 - Toxicity       -> Toxicity
        01. Toxicity        -> Toxicity
        7_Toxicity          -> Toxicity
    """

    return re.sub(
        r"^\s*\d{1,3}[\s._-]+",
        "",
        value,
    ).strip()


def _extract_filename_title(filename):
    """
    Extract the most likely track title from the filename.

    This intentionally does NOT consider the parent directory or album
    name to be the track title.

    Examples:

        01 - Toxicity.flac
            -> Toxicity

        System Of A Down - Toxicity.flac
            -> Toxicity

        Foo - Everlong - 01 - Everlong.flac
            -> Everlong
    """

    name = Path(filename).stem.strip()

    # First remove a leading track number.
    name = _strip_track_number(name)

    # If the filename contains a conventional "Artist - Title" format,
    # use the final component as the likely title.
    parts = [
        part.strip()
        for part in re.split(r"\s+-\s+", name)
        if part.strip()
    ]

    if len(parts) >= 2:
        name = parts[-1]

        # A trailing track number may still exist.
        name = re.sub(
            r"\s*[-._ ]+\s*\d{1,3}$",
            "",
            name,
        ).strip()

    return name


def _filename_components(filename):
    """
    Return useful normalized components from a Soulseek filename/path.

    We intentionally inspect the basename for title matching and the
    complete path only for supplementary artist matching.
    """

    path_normalized = _normalise_metadata(
        filename
    )

    basename = Path(
        filename.replace("\\", "/")
    ).name

    extracted_title = _extract_filename_title(
        basename
    )

    return (
        path_normalized,
        _normalise_track_title(extracted_title),
    )


def _result_matches_track(file, artist, title):
    """
    Determine whether a Soulseek result actually represents the requested
    artist/title.

    IMPORTANT:
        Album/directory names are NOT treated as track titles.

    This prevents:

        Hail to the King/
            01 - Shepherd of Fire.flac

    from being selected for:

        Avenged Sevenfold — Hail to the King
    """

    filename = file.get(
        "filename",
        "",
    )

    if _ext(filename) not in AUDIO_EXTENSIONS:
        return False

    if file.get("isLocked", False):
        return False

    requested_artist = _normalise_artist_for_matching(
        artist
    )

    requested_title = _normalise_track_title(
        title
    )

    path_normalized, filename_title = (
        _filename_components(filename)
    )

    # ---------------------------------------------------------------
    # 1. Exact filename-derived title.
    #
    # This is the strongest result when metadata isn't available.
    # ---------------------------------------------------------------

    if filename_title == requested_title:
        return True

    # ---------------------------------------------------------------
    # 2. Conventional "Artist - Title" filename.
    #
    # The extracted title is already checked above. This branch mainly
    # documents the intended behavior and makes the matching explicit.
    # ---------------------------------------------------------------

    filename_without_number = _strip_track_number(
        Path(filename.replace("\\", "/")).stem
    )

    parts = [
        part.strip()
        for part in re.split(
            r"\s+-\s+",
            filename_without_number,
        )
        if part.strip()
    ]

    if len(parts) >= 2:
        filename_artist = _normalise_artist_for_matching(
            parts[-2]
        )

        filename_title_candidate = _normalise_track_title(
            parts[-1]
        )

        if (
            filename_title_candidate == requested_title
            and _artist_matches(
                requested_artist,
                filename_artist,
            )
        ):
            return True

    # ---------------------------------------------------------------
    # 3. Do NOT accept merely because the title appears in the path.
    #
    # This is the bug that allowed:
    #
    #   Hail to the King/Shepherd of Fire.flac
    #
    # to pass.
    # ---------------------------------------------------------------

    return False


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

    ext = _ext(
        file["filename"]
    )

    if ext == ".flac":
        return 0

    if ext == ".mp3":
        bitrate = (
            file.get("bitRate", 0)
            or 0
        )

        if bitrate >= 320:
            return 1

        return 2

    if ext in AUDIO_EXTENSIONS:
        return 3

    return 99


def pick_best(files, artist, title):
    """
    Pick the best usable Soulseek result that actually matches
    the requested track.
    """

    matching = [
        file
        for file in files
        if _result_matches_track(
            file,
            artist,
            title,
        )
    ]

    if not matching:
        log.warning(
            "No matching Soulseek result for: %s — %s",
            artist,
            title,
        )

        return None

    log.info(
        "Found %d matching results for %s — %s",
        len(matching),
        artist,
        title,
    )

    return sorted(
        matching,
        key=_rank,
    )[0]


# ---------------------------------------------------------------------------
# Filename handling
# ---------------------------------------------------------------------------

def sanitize_filename(value):
    """
    Make a string safe to use as a Unix filename.
    """

    value = value.strip()

    value = re.sub(
        r"[\/\\:*?\"<>|]",
        "_",
        value,
    )

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
        match = re.search(
            pattern,
            name,
        )

        if match:
            number = int(
                match.group(1)
            )

            if 1 <= number <= 99:
                return f"{number:02d}"

    return None


def build_destination(
    artist,
    title,
    source_filename,
):
    """
    Build:

        /data/music/<Artist>/<NN>. <Title>.<ext>
    """

    artist_dir = (
        MUSIC_DIR
        / sanitize_filename(artist)
    )

    extension = _ext(
        source_filename
    )

    track_number = extract_track_number(
        source_filename
    )

    safe_title = sanitize_filename(
        title
    )

    if track_number:
        filename = (
            f"{track_number}. "
            f"{safe_title}"
            f"{extension}"
        )
    else:
        filename = (
            f"{safe_title}"
            f"{extension}"
        )

    return artist_dir / filename


# ---------------------------------------------------------------------------
# Download handling
# ---------------------------------------------------------------------------

def snapshot_download_files():
    """
    Take a snapshot of files currently in the slskd completed directory.
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
    """

    username = file.get(
        "_username"
    )

    filename = file.get(
        "filename"
    )

    size = file.get(
        "size"
    )

    if not username:
        raise RuntimeError(
            "Search result has no username"
        )

    if not filename:
        raise RuntimeError(
            "Search result has no filename"
        )

    log.info(
        "Queuing download: %s from %s",
        filename,
        username,
    )

    resp = requests.post(
        f"{SLSKD_URL}/api/v0/transfers/"
        f"downloads/{username}",
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
    """

    remote_basename = Path(
        remote_filename.replace(
            "\\",
            "/",
        )
    ).name

    return path.name == remote_basename


def wait_for_download(
    file,
    previous_files,
    timeout=DOWNLOAD_TIMEOUT,
):
    """
    Wait until the requested file appears in the completed directory.
    """

    remote_filename = file["filename"]
    expected_size = file.get("size")

    deadline = (
        time.monotonic()
        + timeout
    )

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

            if not _filename_matches(
                path,
                remote_filename,
            ):
                continue

            candidates.append(path)

        for path in candidates:
            try:
                size = path.stat().st_size
            except OSError:
                continue

            if expected_size and size != expected_size:
                continue

            log.info(
                "Download completed: %s",
                path,
            )

            return path

        time.sleep(2)

    raise TimeoutError(
        f"Timed out waiting for download: "
        f"{remote_filename}"
    )


# ---------------------------------------------------------------------------
# Download verification
# ---------------------------------------------------------------------------

def verify_downloaded_file(
    path,
    artist,
    title,
):
    """
    Verify downloaded audio metadata.

    Artist matching allows featured artists.
    Title matching remains strict.
    """

    metadata = read_audio_metadata(
        path
    )

    if metadata is None:
        log.warning(
            "Downloaded file has no readable metadata: %s",
            path,
        )

        return True

    actual_artist, actual_title = metadata

    expected_title = _normalise_track_title(
        title
    )

    actual_title_normalized = _normalise_track_title(
        actual_title
    )

    if not _artist_matches(
        artist,
        actual_artist,
    ):
        log.error(
            "Downloaded artist mismatch: "
            "expected %r, got %r: %s",
            _normalise_artist_for_matching(artist),
            _normalise_artist_for_matching(actual_artist),
            path,
        )

        return False

    if actual_title_normalized != expected_title:
        log.error(
            "Downloaded title mismatch: "
            "expected %r, got %r: %s",
            expected_title,
            actual_title_normalized,
            path,
        )

        return False

    return True


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def ensure_directory_writable(directory):
    """
    Ensure the destination directory is group-writable.

    This is intended for the shared `media` group used by Lidarr,
    Soulbrainz and other music services.

    We do NOT use chmod 777.

    Existing Lidarr-created directories can be 0755, which means a
    Soulbrainz process in the `media` group still cannot write there.
    """

    directory = Path(directory)

    directory.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:
        mode = directory.stat().st_mode
    except OSError as exc:
        raise RuntimeError(
            f"Cannot stat destination directory {directory}: {exc}"
        ) from exc

    # Add group rwx while preserving existing owner/other permissions.
    new_mode = mode | 0o070

    # Setgid ensures children inherit the directory's group.
    new_mode |= 0o2000

    if new_mode != mode:
        try:
            directory.chmod(new_mode)
        except PermissionError as exc:
            raise PermissionError(
                f"Cannot make music directory group-writable: "
                f"{directory}. "
                f"Make sure soulbrainz owns it or has sufficient "
                f"permissions."
            ) from exc


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

    # -----------------------------------------------------------------------
    # Fix for Lidarr-created 0755 directories.
    #
    # Example:
    #
    #   lidarr:media  drwxr-sr-x  Nothing But Thieves
    #
    # becomes group-writable so a media-group Soulbrainz process can import.
    # -----------------------------------------------------------------------

    ensure_directory_writable(
        destination.parent
    )

    if destination.exists():
        log.warning(
            "Destination already exists, skipping import: %s",
            destination,
        )

        return True

    log.info(
        "Importing: %s -> %s",
        source,
        destination,
    )

    shutil.move(
        str(source),
        str(destination),
    )

    cleanup_empty_parents(
        source.parent
    )

    return True


def cleanup_empty_parents(directory):
    """
    Remove empty directories beneath DOWNLOAD_DIR.
    """

    directory = Path(directory)

    while (
        directory != DOWNLOAD_DIR
        and DOWNLOAD_DIR in directory.parents
    ):
        try:
            directory.rmdir()
        except OSError:
            break

        directory = directory.parent


# ---------------------------------------------------------------------------
# Track processing
# ---------------------------------------------------------------------------

def process_track(
    track,
    library_index,
):
    """
    Process one Weekly Jams track.

    Returns:
        "imported"
        "skipped"
        "failed"
    """

    artist = track["artist"]
    title = track["title"]

    log.info(
        "Processing: %s — %s",
        artist,
        title,
    )

    # -----------------------------------------------------------------------
    # 1. Check library.
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

        return "skipped"

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

        return "skipped"

    # -----------------------------------------------------------------------
    # 3. Select the best result.
    # -----------------------------------------------------------------------

    selected = pick_best(
        files,
        artist,
        title,
    )

    if not selected:
        log.warning(
            "No usable audio result: %s — %s",
            artist,
            title,
        )

        return "skipped"

    log.info(
        "Selected: %s from %s",
        selected["filename"],
        selected.get("_username"),
    )

    extension = _ext(
        selected["filename"]
    )

    if extension == ".flac":
        log.info(
            "Selected quality: FLAC"
        )

    elif extension == ".mp3":
        log.info(
            "Selected quality: MP3 %s kbps",
            selected.get(
                "bitRate",
                "?",
            ),
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

    queue_download(
        selected
    )

    # -----------------------------------------------------------------------
    # 6. Wait for download.
    # -----------------------------------------------------------------------

    try:
        completed_file = wait_for_download(
            selected,
            previous_files,
        )

    except TimeoutError as exc:
        log.error(
            "%s",
            exc,
        )

        return "failed"

    # -----------------------------------------------------------------------
    # 7. Verify downloaded file.
    # -----------------------------------------------------------------------

    if not verify_downloaded_file(
        completed_file,
        artist,
        title,
    ):
        log.error(
            "Downloaded file failed metadata verification, "
            "leaving it in the slskd completed directory: %s",
            completed_file,
        )

        return "failed"

    # -----------------------------------------------------------------------
    # 8. Import.
    # -----------------------------------------------------------------------

    try:
        imported = import_download(
            completed_file,
            artist,
            title,
        )

    except Exception:
        log.exception(
            "Failed importing %s — %s",
            artist,
            title,
        )

        return "failed"

    if imported:
        library_index.add(
            (
                _normalise_artist_for_matching(artist),
                _normalise_track_title(title),
            )
        )

        return "imported"

    return "skipped"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info(
        "Starting soulbrainz"
    )

    # -----------------------------------------------------------------------
    # Build library index.
    # -----------------------------------------------------------------------

    library_index = build_library_index()

    # -----------------------------------------------------------------------
    # Fetch Weekly Jams.
    # -----------------------------------------------------------------------

    tracks = get_weekly_jams()

    if not tracks:
        log.info(
            "No tracks to process"
        )

        return

    imported = 0
    skipped = 0
    failed = 0

    # -----------------------------------------------------------------------
    # Process recommendations.
    # -----------------------------------------------------------------------

    for track in tracks:
        try:
            result = process_track(
                track,
                library_index,
            )

            if result == "imported":
                imported += 1

            elif result == "skipped":
                skipped += 1

            elif result == "failed":
                failed += 1

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
        imported,
        skipped,
        failed,
    )


if __name__ == "__main__":
    main()
