import logging
import time
import uuid

import requests

LB_USERNAME = "reisdro"
SLSKD_URL = "https://slskd.voldsoy.duckdns.org"
LB_API = "https://api.listenbrainz.org/1"
SEARCH_TIMEOUT = 30
AUDIO_EXTENSIONS = {".flac", "mp3", ".ogg", ".opus", ".m4a"}

logging.basicConfig(level=logging.INFO, format="%(levelname)s $(message)s")
log = logging.getLogger(__name__)


def _slskd_headers():
    return {}


def fetch_weekly_jams():
    """Return list of (artist, title) tuples from the most recent Weekly Jams playlist."""
    resp = requests.get(
        f"{LB_API}/user/{LB_USERNAME}/playlists/createdfor",
        timeout=15,
    )
    resp.raise_for_status()
    playlists = resp.json().get("playlists", [])

    weekly = next(
        (p for p in playlists if "Weekly Jams" in p["playlists"].get("title", "")),
        None,
    )
    if weekly is None:
        log.warning("No weekly Jams palylist found for user %s", LB_USERNAME)
        return []

    mbid = weekly["playlist"]["identifier"].rstrip("/").split("/")[-1]
    log.info("Fetching playlist %s", mbid)

    resp = requests.get(f"{LB_API}/playlist/{mbid}", timeout=15)
    resp.raise_for_status

    tracks = resp.json().get("playlist", {}).get("track", [])
    return [
        (t.get("creator", ""), t.get("title", "")) for t in tracks if t.get("title")
    ]


def search_slskd(artist, title):
    """Submit a search to slskd and return the list of file results."""
    search_id = str(uuid.uuid4())
    query = f"{artist} {title}".strip()

    requests.post(
        f"{SLSKD_URL}/api/v0/searches",
        json={"id": search_id, "searchText": query},
        headers=_slskd_headers(),
        timeout=10,
    ).raise_for_status()

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
        if data.get("state") == "Completed":
            files = []
            for response in data.get("responses", []):
                for f in response.get("files", []):
                    f["_username"] = response["username"]
                    files.append(f)
                return files
    log.warning("Search timed out for %s", query)
    return []


def _ext(filename):
    dot = filename.rfind(".")
    return filename[dot:].lower() if dot != -1 else ""


def _rank(f):
    ext = _ext(f["filename"])
    if ext == ".flac":
        return 0
    if ext == ".mp3":
        bitrate = f.get("bitRate", 0) or 0
        return 1 if bitrate >= 320 else 2
    if ext in AUDIO_EXTENSIONS:
        return 3
    return 99


def pick_best(files):
    """Return the best audio file from a list of slskd search result files, or None."""
    audio = [f for f in files if _ext(f["filename"]) in AUDIO_EXTENSIONS]
    if not audio:
        return None
    return sorted(audio, key=_rank)[0]


def download(file):
    username = file["_username"]
    requests.post(
        f"{SLSKD_URL}/api/v0/transfers/downloads/{username}",
        json={"filename": file["filename"], "size": file.get("size", 0)},
        headers=_slskd_headers(),
        timeout=10,
    ).raise_for_status()


def main():
    log.info("Fetching Weekly Jams for %s", LB_USERNAME)
    tracks = fetch_weekly_jams()
    log.info("Found %d tracks", len(tracks))

    for artist, title in tracks:
        log.info("Searching: %s - %s", artist, title)
        files = search_slskd(artist, title)
        best = pick_best(files)
        if best is None:
            log.warning("No audio result for: %s - %s", artist, title)
            continue
        log.info("Queuing download: %s from %s", best["filename"], best["_username"])
        try:
            download(best)
        except requests.HTTPError as exc:
            log.warning("Download request failed for %s - %s: %s", artist, title, exc)
    log.info("Done")


if __name__ == "__main__":
    main()
