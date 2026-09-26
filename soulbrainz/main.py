#!/usr/bin/env python3

from __future__ import annotations

import logging
import os
import re
import shutil
import tempfile
import time
import unicodedata
import uuid
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import requests
from mutagen import File as MutagenFile

LISTENBRAINZ_URL = "https://api.listenbrainz.org/1"
DEFAULT_SLSKD_URL = "https://slskd.voldsoy.duckdns.org"
AUDIO_EXTENSIONS = {
    ".aac",
    ".alac",
    ".flac",
    ".m4a",
    ".mp3",
    ".ogg",
    ".opus",
    ".wav",
}

log = logging.getLogger("soulbrainz")


@dataclass(frozen=True)
class Config:
    listenbrainz_user: str
    slskd_url: str
    slskd_api_key: str | None = field(repr=False)
    music_dir: Path
    download_dir: Path
    plex_url: str | None = None
    plex_token: str | None = field(default=None, repr=False)
    plex_library: str = "Music"
    playlist_dir: Path | None = None
    plex_scan_timeout: float = 300
    search_timeout: float = 60
    download_timeout: float = 300
    poll_interval: float = 2

    @classmethod
    def from_env(cls) -> Config:
        music_dir = Path(os.environ.get("MUSIC_DIR", "/data/music"))
        plex_url = os.environ.get("PLEX_URL")
        return cls(
            listenbrainz_user=os.environ.get("LISTENBRAINZ_USER", "reisdro"),
            slskd_url=os.environ.get("SLSKD_URL", DEFAULT_SLSKD_URL).rstrip("/"),
            slskd_api_key=os.environ.get("SLSKD_API_KEY"),
            music_dir=music_dir,
            download_dir=Path(
                os.environ.get(
                    "DOWNLOAD_DIR",
                    "/data/downloads/slskd/complete",
                )
            ),
            plex_url=plex_url.rstrip("/") if plex_url else None,
            plex_token=_load_plex_token() if plex_url else None,
            plex_library=os.environ.get("PLEX_LIBRARY", "Music"),
            playlist_dir=Path(
                os.environ.get("PLAYLIST_DIR", music_dir / ".playlists")
            ),
        )


@dataclass(frozen=True)
class Track:
    artist: str
    title: str


@dataclass(frozen=True)
class WeeklyJams:
    playlist_id: str
    published: date
    tracks: tuple[Track, ...]

    @property
    def title(self) -> str:
        return f"Weekly Jams — {self.published.isoformat()}"

    @property
    def filename(self) -> str:
        return f"weekly-jams-{self.published.isoformat()}.m3u8"


class InvalidAudio(ValueError):
    pass


def _load_plex_token() -> str | None:
    if token := os.environ.get("PLEX_TOKEN"):
        return token

    preferences_file = os.environ.get("PLEX_PREFERENCES_FILE")
    if not preferences_file and (credentials_dir := os.environ.get("CREDENTIALS_DIRECTORY")):
        preferences_file = str(Path(credentials_dir) / "plex-preferences")
    if not preferences_file or not Path(preferences_file).is_file():
        return None

    try:
        preferences = ElementTree.parse(preferences_file).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise RuntimeError(f"Could not read Plex credentials: {exc}") from exc
    return preferences.get("PlexOnlineToken")


def _slskd_headers(config: Config) -> dict[str, str]:
    headers = {"Accept": "application/json"}
    if config.slskd_api_key:
        headers["X-API-Key"] = config.slskd_api_key
    return headers


def _is_weekly_jams(playlist: dict[str, Any], username: str) -> bool:
    metadata = (
        playlist.get("extension", {})
        .get("https://musicbrainz.org/doc/jspf#playlist", {})
        .get("additional_metadata", {})
        .get("algorithm_metadata", {})
    )
    return metadata.get("source_patch") == "weekly-jams" or playlist.get(
        "title", ""
    ).startswith(f"Weekly Jams for {username}")


def get_weekly_jams(config: Config, http: requests.Session) -> WeeklyJams:
    """Fetch the newest Weekly Jams playlist for the configured user."""
    response = http.get(
        f"{LISTENBRAINZ_URL}/user/{quote(config.listenbrainz_user, safe='')}/"
        "playlists/createdfor",
        timeout=20,
    )
    response.raise_for_status()

    playlists = [
        entry.get("playlist", {})
        for entry in response.json().get("playlists", [])
        if isinstance(entry, dict)
    ]
    candidates = [
        playlist
        for playlist in playlists
        if _is_weekly_jams(playlist, config.listenbrainz_user)
    ]
    if not candidates:
        raise RuntimeError("Could not find a Weekly Jams playlist")

    playlist = max(candidates, key=lambda item: item.get("date", ""))
    identifier = playlist.get("identifier", "")
    if not isinstance(identifier, str) or not identifier.rstrip("/"):
        raise RuntimeError("Weekly Jams playlist has no identifier")
    playlist_id = identifier.rstrip("/").rsplit("/", 1)[-1]
    try:
        published = date.fromisoformat(str(playlist.get("date", ""))[:10])
    except ValueError as exc:
        raise RuntimeError("Weekly Jams playlist has no valid date") from exc

    response = http.get(
        f"{LISTENBRAINZ_URL}/playlist/{quote(playlist_id, safe='')}",
        timeout=20,
    )
    response.raise_for_status()

    tracks: list[Track] = []
    for item in response.json().get("playlist", {}).get("track", []):
        artist = item.get("creator")
        title = item.get("title")
        if artist and title:
            tracks.append(Track(str(artist).strip(), str(title).strip()))

    log.info("Found %d tracks in Weekly Jams %s", len(tracks), playlist_id)
    return WeeklyJams(playlist_id, published, tuple(tracks))


def normalize(value: Any) -> str:
    value = unicodedata.normalize("NFKD", str(value or "").casefold())
    value = "".join(character for character in value if not unicodedata.combining(character))
    value = value.replace("&", " and ")
    return re.sub(r"[\W_]+", " ", value, flags=re.UNICODE).strip()


def artist_key(value: Any) -> str:
    value = re.split(
        r"\b(?:feat|ft|featuring)\b",
        normalize(value),
        maxsplit=1,
    )[0].strip()
    inverted = re.fullmatch(r"(.+) (the|a|an)", value)
    return f"{inverted.group(2)} {inverted.group(1)}" if inverted else value


def title_key(value: Any) -> str:
    return re.sub(
        r"\s+(?:feat|ft|featuring)\s+.+$",
        "",
        normalize(value),
    ).strip()


def track_key(artist: Any, title: Any) -> tuple[str, str]:
    return artist_key(artist), title_key(title)


def _first_tag(audio: Any, *keys: str) -> str | None:
    if not getattr(audio, "tags", None):
        return None
    for key in keys:
        value = audio.tags.get(key)
        if isinstance(value, list):
            value = value[0] if value else None
        if value:
            return str(value)
    return None


def read_audio_metadata(path: Path) -> tuple[str | None, str | None]:
    """Return available tags, or raise when the file is not valid audio."""
    try:
        audio = MutagenFile(path, easy=True)
    except Exception as exc:
        raise InvalidAudio(f"Could not read audio file {path}: {exc}") from exc
    if audio is None:
        raise InvalidAudio(f"Unsupported or corrupt audio file: {path}")

    artist = _first_tag(audio, "artist", "albumartist")
    title = _first_tag(audio, "title")
    return artist, title


def _title_from_library_filename(filename: str) -> str:
    return _strip_track_number(Path(filename).stem)


def build_library_index(config: Config) -> dict[tuple[str, str], Path]:
    """Index valid audio by tags, falling back to Artist/.../Title paths."""
    index: dict[tuple[str, str], Path] = {}
    if not config.music_dir.exists():
        log.warning("Music directory does not exist: %s", config.music_dir)
        return index

    file_count = 0
    for path in sorted(config.music_dir.rglob("*")):
        if not path.is_file() or path.suffix.casefold() not in AUDIO_EXTENSIONS:
            continue
        file_count += 1
        try:
            metadata = read_audio_metadata(path)
        except InvalidAudio as exc:
            log.warning("%s", exc)
            continue

        artist, title = metadata
        if artist and title:
            index.setdefault(track_key(*metadata), path)
            continue

        relative = path.relative_to(config.music_dir)
        if len(relative.parts) > 1:
            index.setdefault(
                track_key(
                    artist or relative.parts[0],
                    title or _title_from_library_filename(path.name),
                ),
                path,
            )

    log.info("Indexed %d audio files (%d unique tracks)", file_count, len(index))
    return index


def search_slskd(
    config: Config,
    http: requests.Session,
    track: Track,
) -> list[dict[str, Any]]:
    search_id = str(uuid.uuid4())
    response = http.post(
        f"{config.slskd_url}/api/v0/searches",
        json={"id": search_id, "searchText": f"{track.artist} {track.title}"},
        headers=_slskd_headers(config),
        timeout=10,
    )
    response.raise_for_status()

    deadline = time.monotonic() + config.search_timeout
    while time.monotonic() < deadline:
        time.sleep(config.poll_interval)
        response = http.get(
            f"{config.slskd_url}/api/v0/searches/{search_id}",
            headers=_slskd_headers(config),
            timeout=10,
        )
        response.raise_for_status()
        if not response.json().get("isComplete"):
            continue

        response = http.get(
            f"{config.slskd_url}/api/v0/searches/{search_id}/responses",
            headers=_slskd_headers(config),
            timeout=10,
        )
        response.raise_for_status()
        files = []
        for result in response.json():
            username = result.get("username")
            files.extend(
                {
                    **item,
                    "_username": username,
                    "_has_free_slot": result.get("hasFreeUploadSlot"),
                    "_queue_length": result.get("queueLength"),
                    "_upload_speed": result.get("uploadSpeed"),
                }
                for item in result.get("files", [])
            )
        log.info(
            "Search returned %d files for %s — %s",
            len(files),
            track.artist,
            track.title,
        )
        return files

    log.warning("Search timed out for %s — %s", track.artist, track.title)
    return []


def _remote_path(filename: str) -> PurePosixPath:
    return PurePosixPath(filename.replace("\\", "/"))


def _strip_track_number(value: str) -> str:
    return re.sub(
        r"^\s*(?:(?:cd|disc)\s*|[a-d])?\d{1,3}(?:[-.]\d{1,3})?[\s._-]+",
        "",
        value,
        flags=re.IGNORECASE,
    ).strip()


def _contains_phrase(value: str, phrase: str) -> bool:
    return bool(phrase) and f" {phrase} " in f" {normalize(value)} "


def result_confidence(file: dict[str, Any], track: Track) -> int:
    """Return 0 for no match, 1 for path artist, 2 for filename artist."""
    filename = str(file.get("filename", ""))
    path = _remote_path(filename)
    if path.suffix.casefold() not in AUDIO_EXTENSIONS or file.get("isLocked"):
        return 0

    raw_stem = path.stem
    stem = _strip_track_number(raw_stem)
    parts = [part.strip() for part in re.split(r"\s+-\s+", stem) if part.strip()]
    underscore_parts = [part.strip() for part in raw_stem.split("_") if part.strip()]
    expected_artist = artist_key(track.artist)
    filename_artist = len(parts) > 1 and artist_key(parts[0]) == expected_artist
    underscore_artist = (
        len(underscore_parts) > 1
        and artist_key(underscore_parts[0]) == expected_artist
    )
    path_artist = any(
        artist_key(part) == expected_artist for part in path.parts[:-1]
    ) or _contains_phrase("/".join(path.parts[:-1]), expected_artist)
    confidence = 2 if filename_artist or underscore_artist else 1 if path_artist else 0
    if not confidence:
        return 0

    candidates = {title_key(stem), title_key(raw_stem)}
    if filename_artist and len(parts) > 1:
        candidates.add(title_key(" - ".join(parts[1:])))
        candidates.add(title_key(parts[-1]))
    elif path_artist and any(
        re.fullmatch(r"(?:track )?\d{1,3}", normalize(part))
        for part in parts[:-1]
    ):
        candidates.add(title_key(parts[-1]))
    if underscore_artist:
        for index, part in enumerate(underscore_parts):
            if normalize(part).isdigit() and index + 1 < len(underscore_parts):
                candidates.add(title_key("_".join(underscore_parts[index + 1 :])))
    return confidence if title_key(track.title) in candidates else 0


def _quality_rank(file: dict[str, Any]) -> tuple[int, int]:
    extension = _remote_path(str(file.get("filename", ""))).suffix.casefold()
    bitrate = int(file.get("bitRate") or 0)
    if extension == ".flac":
        return 0, 0
    if extension == ".mp3":
        return (1 if bitrate >= 320 else 2), -bitrate
    return 3, -bitrate


def _availability_rank(file: dict[str, Any]) -> tuple[int, int, int]:
    free_slot = file.get("_has_free_slot")
    slot_rank = 0 if free_slot is True else 2 if free_slot is False else 1
    queue_length = int(file.get("_queue_length") or 0)
    upload_speed = int(file.get("_upload_speed") or 0)
    return slot_rank, queue_length, -upload_speed


def pick_best(files: list[dict[str, Any]], track: Track) -> dict[str, Any] | None:
    matches = [(result_confidence(file, track), file) for file in files]
    matches = [(confidence, file) for confidence, file in matches if confidence]
    if not matches:
        return None
    return min(
        matches,
        key=lambda item: (
            -item[0],
            *_quality_rank(item[1]),
            *_availability_rank(item[1]),
        ),
    )[1]


def download_snapshot(directory: Path) -> dict[Path, tuple[int, int, int]]:
    snapshot = {}
    if not directory.exists():
        return snapshot
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        snapshot[path] = (stat.st_ino, stat.st_size, stat.st_mtime_ns)
    return snapshot


def queue_download(
    config: Config,
    http: requests.Session,
    file: dict[str, Any],
) -> None:
    username = file.get("_username")
    filename = file.get("filename")
    if not username or not filename:
        raise RuntimeError("Search result is missing a username or filename")
    response = http.post(
        f"{config.slskd_url}/api/v0/transfers/downloads/{quote(str(username), safe='')}",
        json=[{"filename": filename, "size": file.get("size")}],
        headers=_slskd_headers(config),
        timeout=10,
    )
    response.raise_for_status()


def _download_name_matches(path: Path, remote_filename: str) -> bool:
    expected = _remote_path(remote_filename).name
    if path.name.casefold() == expected.casefold():
        return True
    expected_path = Path(expected)
    return (
        path.suffix.casefold() == expected_path.suffix.casefold()
        and re.fullmatch(
            rf"{re.escape(expected_path.stem)}(?: \(\d+\)|_\d+)",
            path.stem,
            flags=re.IGNORECASE,
        )
        is not None
    )


def wait_for_download(
    config: Config,
    file: dict[str, Any],
    before: dict[Path, tuple[int, int, int]],
) -> Path:
    remote_filename = str(file["filename"])
    expected_size = int(file.get("size") or 0)
    deadline = time.monotonic() + config.download_timeout

    while True:
        current = download_snapshot(config.download_dir)
        candidates = [
            path
            for path, signature in current.items()
            if before.get(path) != signature
            and _download_name_matches(path, remote_filename)
            and (not expected_size or signature[1] == expected_size)
        ]
        if candidates:
            return max(candidates, key=lambda path: current[path][2])
        if time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for download: {remote_filename}")
        time.sleep(config.poll_interval)


def verify_downloaded_file(path: Path, track: Track) -> bool:
    try:
        metadata = read_audio_metadata(path)
    except InvalidAudio as exc:
        log.error("%s", exc)
        return False
    artist, title = metadata
    if not artist and not title:
        log.warning("Downloaded audio has no artist/title tags: %s", path)
    if artist and artist_key(artist) != artist_key(track.artist):
        return False
    if title and title_key(title) != title_key(track.title):
        return False
    return True


def sanitize_filename(value: str) -> str:
    value = re.sub(r"[/\\:*?\"<>|]", "_", value.strip()).rstrip(" .")
    return value or "Unknown"


def extract_track_number(filename: str) -> str | None:
    stem = Path(filename).stem
    patterns = (
        r"^\s*\d{1,2}[-.](\d{1,2})(?:\D|$)",
        r"^\s*(?:track\s*|[a-d])(\d{1,2})(?:\D|$)",
        r"^\s*(\d{1,2})[\s._-]+",
        r"\s-\s(\d{1,2})\s-\s",
        r"_(\d{1,2})_",
    )
    for pattern in patterns:
        match = re.search(pattern, stem, flags=re.IGNORECASE)
        if match and 1 <= int(match.group(1)) <= 99:
            return f"{int(match.group(1)):02d}"
    return None


def build_destination(config: Config, track: Track, source: Path) -> Path:
    track_number = extract_track_number(source.name)
    prefix = f"{track_number}. " if track_number else ""
    return (
        config.music_dir
        / sanitize_filename(track.artist)
        / f"{prefix}{sanitize_filename(track.title)}{source.suffix.casefold()}"
    )


def _cleanup_empty_parents(directory: Path, root: Path) -> None:
    while directory != root and root in directory.parents:
        try:
            directory.rmdir()
        except OSError:
            return
        directory = directory.parent


def import_download(config: Config, source: Path, track: Track) -> Path | None:
    destination = build_destination(config, track, source)
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o775)
    if destination.exists():
        log.warning("Destination already exists; leaving download at %s", source)
        return None
    if not os.access(destination.parent, os.W_OK):
        raise PermissionError(f"Music directory is not writable: {destination.parent}")

    temporary: Path | None = None
    published = False
    try:
        with tempfile.NamedTemporaryFile(
            dir=destination.parent,
            prefix=".soulbrainz-",
            delete=False,
        ) as output_file:
            temporary = Path(output_file.name)
            with source.open("rb") as input_file:
                shutil.copyfileobj(input_file, output_file)
            output_file.flush()
            os.fsync(output_file.fileno())

        temporary.chmod(0o664)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            log.warning("Destination appeared during import; leaving %s", source)
            return None
        published = True
        source.unlink()
    except Exception:
        if published:
            destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)

    _cleanup_empty_parents(source.parent, config.download_dir)
    log.info("Imported %s — %s to %s", track.artist, track.title, destination)
    return destination


def process_track(
    config: Config,
    http: requests.Session,
    track: Track,
    library: dict[tuple[str, str], Path],
) -> str:
    if track_key(track.artist, track.title) in library:
        log.info("Already in library: %s — %s", track.artist, track.title)
        return "skipped"

    selected = pick_best(search_slskd(config, http, track), track)
    if not selected:
        log.warning("No matching result: %s — %s", track.artist, track.title)
        return "skipped"

    before = download_snapshot(config.download_dir)
    queue_download(config, http, selected)
    downloaded = wait_for_download(config, selected, before)
    if not verify_downloaded_file(downloaded, track):
        log.error("Downloaded file did not match: %s", downloaded)
        return "failed"
    destination = import_download(config, downloaded, track)
    if destination is None:
        return "skipped"

    library[track_key(track.artist, track.title)] = destination
    return "imported"


def _plex_headers(config: Config) -> dict[str, str]:
    if not config.plex_token:
        raise RuntimeError(
            "Plex publishing requires PLEX_TOKEN or the plex-preferences credential"
        )
    return {
        "Accept": "application/json",
        "X-Plex-Token": config.plex_token,
        "X-Plex-Client-Identifier": "soulbrainz",
        "X-Plex-Product": "Soulbrainz",
    }


def _plex_objects(response: requests.Response, key: str) -> list[dict[str, Any]]:
    container = response.json().get("MediaContainer", {})
    items = container.get(key, []) if isinstance(container, dict) else []
    if isinstance(items, dict):
        items = [items]
    return [item for item in items if isinstance(item, dict)]


def _plex_collection(
    config: Config,
    http: requests.Session,
    path: str,
    key: str,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Read every page of a Plex collection."""
    items: list[dict[str, Any]] = []
    start = 0
    page_size = 100
    while True:
        page_params = dict(params or {})
        page_params.update(
            {
                "X-Plex-Container-Start": start,
                "X-Plex-Container-Size": page_size,
            }
        )
        response = http.get(
            f"{config.plex_url}{path}",
            params=page_params,
            headers=_plex_headers(config),
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        container = payload.get("MediaContainer", {})
        if not isinstance(container, dict):
            raise RuntimeError(f"Plex returned an invalid collection for {path}")
        page = _plex_objects(response, key)
        items.extend(page)

        total_value = container.get("totalSize")
        try:
            total = int(total_value) if total_value is not None else None
        except (TypeError, ValueError):
            total = None
        start += len(page)
        if (
            not page
            or (total is not None and start >= total)
            or (total is None and len(page) < page_size)
        ):
            return items


def _plex_music_section(
    config: Config,
    http: requests.Session,
) -> dict[str, Any]:
    response = http.get(
        f"{config.plex_url}/library/sections",
        headers=_plex_headers(config),
        timeout=20,
    )
    response.raise_for_status()
    sections = [
        section
        for section in _plex_objects(response, "Directory")
        if section.get("type") == "artist"
        and section.get("title") == config.plex_library
    ]
    if len(sections) != 1 or not sections[0].get("key"):
        raise RuntimeError(
            f"Expected one Plex music library named {config.plex_library!r}, "
            f"found {len(sections)}"
        )
    return sections[0]


def _plex_is_refreshing(value: Any) -> bool:
    return value is True or str(value).casefold() in {"1", "true"}


def refresh_plex_library(
    config: Config,
    http: requests.Session,
    section: dict[str, Any],
) -> None:
    section_key = str(section["key"])
    response = http.post(
        f"{config.plex_url}/library/sections/{quote(section_key, safe='')}/refresh",
        params={"path": str(config.music_dir)},
        headers=_plex_headers(config),
        timeout=20,
    )
    response.raise_for_status()

    deadline = time.monotonic() + config.plex_scan_timeout
    headers = getattr(response, "headers", {})
    activity_id = headers.get("X-Plex-Activity") or headers.get("x-plex-activity")
    if activity_id:
        while True:
            response = http.get(
                f"{config.plex_url}/activities",
                headers=_plex_headers(config),
                timeout=20,
            )
            response.raise_for_status()
            active_ids = {
                str(activity.get("uuid"))
                for activity in _plex_objects(response, "Activity")
            }
            if activity_id not in active_ids:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Plex did not finish scanning {config.plex_library!r} "
                    f"within {config.plex_scan_timeout:g} seconds"
                )
            time.sleep(config.poll_interval)

    initial_scanned_at = section.get("scannedAt")
    saw_refresh = False
    registration_deadline = min(
        deadline,
        time.monotonic() + max(1, min(5, config.poll_interval * 2)),
    )
    while True:
        current = _plex_music_section(config, http)
        refreshing = _plex_is_refreshing(current.get("refreshing"))
        saw_refresh = saw_refresh or refreshing
        scan_advanced = (
            initial_scanned_at is not None
            and current.get("scannedAt") != initial_scanned_at
        )
        if not refreshing and (saw_refresh or scan_advanced):
            return
        now = time.monotonic()
        if not refreshing and now >= registration_deadline:
            return
        if now >= deadline:
            raise TimeoutError(
                f"Plex did not finish scanning {config.plex_library!r} "
                f"within {config.plex_scan_timeout:g} seconds"
            )
        time.sleep(config.poll_interval)


def _weekly_paths(
    weekly: WeeklyJams,
    library: dict[tuple[str, str], Path],
) -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for track in weekly.tracks:
        path = library.get(track_key(track.artist, track.title))
        if path is not None and path not in seen:
            seen.add(path)
            paths.append(path)
    return paths


def write_m3u(config: Config, weekly: WeeklyJams, paths: list[Path]) -> Path:
    if not paths:
        raise RuntimeError("None of the recommended tracks are available locally")

    playlist_dir = config.playlist_dir or config.music_dir / ".playlists"
    playlist_dir.mkdir(parents=True, exist_ok=True, mode=0o775)
    destination = playlist_dir / weekly.filename
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=playlist_dir,
            prefix=".soulbrainz-",
            delete=False,
        ) as output_file:
            temporary = Path(output_file.name)
            output_file.write("#EXTM3U\n")
            for path in paths:
                if not path.is_absolute() or "\n" in str(path) or "\r" in str(path):
                    raise ValueError(f"Invalid playlist path: {path}")
                relative_path = os.path.relpath(path, playlist_dir)
                output_file.write(f"{relative_path}\n")
            output_file.flush()
            os.fsync(output_file.fileno())

        temporary.chmod(0o664)
        os.replace(temporary, destination)
        temporary = None
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return destination


def _find_plex_playlist(
    config: Config,
    http: requests.Session,
    m3u_path: Path,
    section_key: str,
) -> dict[str, Any]:
    deadline = time.monotonic() + min(config.plex_scan_timeout, 30)
    while True:
        playlists = _plex_collection(
            config,
            http,
            "/playlists",
            "Metadata",
            {"playlistType": "audio", "sectionID": section_key},
        )
        for playlist in playlists:
            if str(playlist.get("guid", "")).endswith(str(m3u_path)):
                return playlist
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Plex did not import playlist {m3u_path}")
        time.sleep(config.poll_interval)


def _plex_playlist_key(playlist: dict[str, Any]) -> str:
    key = str(playlist.get("key", "")).removesuffix("/items")
    if re.fullmatch(r"/playlists/\d+", key):
        return key
    rating_key = str(playlist.get("ratingKey", ""))
    if rating_key.isdigit():
        return f"/playlists/{rating_key}"
    raise RuntimeError("Imported Plex playlist has no valid key")


def _plex_item_files(item: dict[str, Any]) -> set[str]:
    media_items = item.get("Media", [])
    if isinstance(media_items, dict):
        media_items = [media_items]
    files: set[str] = set()
    for media in media_items if isinstance(media_items, list) else []:
        if not isinstance(media, dict):
            continue
        parts = media.get("Part", [])
        if isinstance(parts, dict):
            parts = [parts]
        for part in parts if isinstance(parts, list) else []:
            if isinstance(part, dict) and part.get("file"):
                files.add(str(part["file"]))
    return files


def wait_for_plex_tracks(
    config: Config,
    http: requests.Session,
    section_key: str,
    paths: list[Path],
) -> None:
    expected = {str(path) for path in paths}
    deadline = time.monotonic() + config.plex_scan_timeout
    missing = expected
    while True:
        tracks = _plex_collection(
            config,
            http,
            f"/library/sections/{quote(section_key, safe='')}/all",
            "Metadata",
            {"type": 10},
        )
        indexed = set().union(*(_plex_item_files(track) for track in tracks))
        missing = expected - indexed
        if not missing:
            return
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"Plex did not index {len(missing)} of {len(expected)} playlist tracks "
                f"within {config.plex_scan_timeout:g} seconds"
            )
        time.sleep(config.poll_interval)


def _plex_playlist_matches(
    config: Config,
    http: requests.Session,
    playlist_key: str,
    paths: list[Path],
) -> tuple[bool, int]:
    items = _plex_collection(
        config,
        http,
        f"{playlist_key}/items",
        "Metadata",
    )
    matches = len(items) == len(paths) and all(
        str(path) in _plex_item_files(item)
        for path, item in zip(paths, items, strict=True)
    )
    return matches, len(items)


def publish_plex_playlist(
    config: Config,
    http: requests.Session,
    weekly: WeeklyJams,
    paths: list[Path],
) -> None:
    m3u_path = write_m3u(config, weekly, paths)
    section = _plex_music_section(config, http)
    section_key = str(section["key"])
    refresh_plex_library(config, http, section)
    wait_for_plex_tracks(config, http, section_key, paths)

    response = http.post(
        f"{config.plex_url}/playlists/upload",
        params={"sectionID": section_key, "path": str(m3u_path)},
        headers=_plex_headers(config),
        timeout=30,
    )
    response.raise_for_status()
    deadline = time.monotonic() + min(config.plex_scan_timeout, 30)
    while True:
        playlist = _find_plex_playlist(
            config,
            http,
            m3u_path,
            section_key,
        )
        playlist_key = _plex_playlist_key(playlist)
        matches, item_count = _plex_playlist_matches(
            config,
            http,
            playlist_key,
            paths,
        )
        if matches:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"Plex playlist has {item_count} items but does not match "
                f"the {len(paths)} tracks in {m3u_path}"
            )
        time.sleep(config.poll_interval)

    if playlist.get("title") != weekly.title:
        response = http.put(
            f"{config.plex_url}{playlist_key}",
            params={"title.value": weekly.title, "title.locked": 1},
            headers=_plex_headers(config),
            timeout=20,
        )
        response.raise_for_status()
    log.info("Published Plex playlist %s with %d tracks", weekly.title, len(paths))


def run(config: Config, http: requests.Session) -> int:
    library = build_library_index(config)
    counts = {"imported": 0, "skipped": 0, "failed": 0}
    weekly = get_weekly_jams(config, http)
    for track in weekly.tracks:
        try:
            result = process_track(config, http, track, library)
        except Exception:
            log.exception("Failed processing %s — %s", track.artist, track.title)
            result = "failed"
        counts[result] += 1

    playlist_failed = False
    if config.plex_url:
        try:
            publish_plex_playlist(config, http, weekly, _weekly_paths(weekly, library))
        except Exception:
            log.exception("Failed publishing %s to Plex", weekly.title)
            playlist_failed = True

    log.info(
        "Finished: %d imported, %d skipped, %d failed",
        counts["imported"],
        counts["skipped"],
        counts["failed"],
    )
    return 1 if counts["failed"] or playlist_failed else 0


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    try:
        with requests.Session() as http:
            return run(Config.from_env(), http)
    except Exception:
        log.exception("Soulbrainz failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
