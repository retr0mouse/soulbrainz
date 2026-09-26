import os
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from soulbrainz import main


class Response:
    def __init__(self, data, *, ok=True, headers=None):
        self.data = data
        self.ok = ok
        self.headers = headers or {}

    def json(self):
        return self.data

    def raise_for_status(self):
        if not self.ok:
            raise RuntimeError("request failed")


def config(tmp_path: Path, **changes) -> main.Config:
    values = {
        "listenbrainz_user": "listener",
        "slskd_url": "http://slskd",
        "slskd_api_key": None,
        "music_dir": tmp_path / "music",
        "download_dir": tmp_path / "downloads",
        "search_timeout": 0.1,
        "download_timeout": 0.1,
        "poll_interval": 0,
    }
    values.update(changes)
    return main.Config(**values)


def test_normalization_and_artist_features():
    assert main.normalize("System_of-a-Down") == "system of a down"
    assert main.normalize("Blood & Water") == "blood and water"
    assert main.normalize("Beyoncé") == "beyonce"
    assert main.artist_key("Wallows feat. Clairo") == "wallows"
    assert main.artist_key("Strokes, The") == "the strokes"
    assert main.title_key("Are You Bored Yet_ feat. Clairo") == "are you bored yet"
    assert main.track_key("Kid Cudi ft CeeLo", "Alive!") == (
        "kid cudi",
        "alive",
    )


def test_get_weekly_jams_uses_newest_algorithm_playlist(tmp_path):
    old = {
        "title": "renamed",
        "date": "2026-01-01",
        "identifier": "https://listenbrainz.org/playlist/old",
        "extension": {
            "https://musicbrainz.org/doc/jspf#playlist": {
                "additional_metadata": {
                    "algorithm_metadata": {"source_patch": "weekly-jams"}
                }
            }
        },
    }
    new = {**old, "date": "2026-02-01", "identifier": "https://x/new"}
    http = Mock()
    http.get.side_effect = [
        Response({"playlists": [{"playlist": old}, {"playlist": new}]}),
        Response(
            {
                "playlist": {
                    "track": [
                        {"creator": " Artist ", "title": " Song "},
                        {"creator": "", "title": "ignored"},
                    ]
                }
            }
        ),
    ]

    assert main.get_weekly_jams(config(tmp_path), http) == main.WeeklyJams(
        "new",
        date(2026, 2, 1),
        (main.Track("Artist", "Song"),),
    )
    assert http.get.call_args_list[1].args[0].endswith("/playlist/new")


def test_load_plex_token_from_systemd_credential(tmp_path):
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    (credentials / "plex-preferences").write_text(
        '<Preferences PlexOnlineToken="credential-token"/>',
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {"CREDENTIALS_DIRECTORY": str(credentials)},
        clear=True,
    ):
        assert main._load_plex_token() == "credential-token"


def test_explicit_plex_token_takes_precedence(tmp_path):
    with patch.dict(
        os.environ,
        {
            "PLEX_TOKEN": "explicit-token",
            "PLEX_PREFERENCES_FILE": str(tmp_path / "missing"),
        },
        clear=True,
    ):
        assert main._load_plex_token() == "explicit-token"


@pytest.mark.parametrize(
    ("filename", "track", "confidence"),
    [
        (r"Beyoncé\I Am\01 - Halo.flac", main.Track("Beyoncé", "Halo"), 1),
        (r"Other Artist\Album\01 - Halo.flac", main.Track("Beyoncé", "Halo"), 0),
        (r"Album\Beyoncé - Halo.flac", main.Track("Beyoncé", "Halo"), 2),
        (r"Artist\Album\01 - Song - Live.flac", main.Track("Artist", "Song - Live"), 1),
        (r"Paramore\Album\1-11 All I Wanted.flac", main.Track("Paramore", "All I Wanted"), 1),
        (r"Tyler\Album\02.12 EARFQUAKE.flac", main.Track("Tyler", "EARFQUAKE"), 1),
        (r"Music\M83\A2 - Midnight City.flac", main.Track("M83", "Midnight City"), 1),
        (r"Green Day\Album\21 Guns.flac", main.Track("Green Day", "21 Guns"), 1),
        (
            r"Music\Strokes, The\2003 - Room On Fire\02. Reptilia.flac",
            main.Track("The Strokes", "Reptilia"),
            1,
        ),
        (
            r"Palaye Royale\Fever Dream (2022) - 03 - No Love in LA.flac",
            main.Track("Palaye Royale", "No Love in LA"),
            1,
        ),
        (
            r"Oliver Tree\Album\Track 14 - I'm Gone.flac",
            main.Track("Oliver Tree", "I'm Gone"),
            1,
        ),
        (
            r"share\Royel Otis_Bar & Grill_01_Oysters in My Pocket.flac",
            main.Track("Royel Otis", "Oysters in My Pocket"),
            2,
        ),
        (
            r"beets\Wallows\Nothing Happens\04 Are You Bored Yet_ feat. Clairo.flac",
            main.Track("Wallows feat. Clairo", "Are You Bored Yet?"),
            1,
        ),
        (
            r"Avenged Sevenfold\Hail to the King\01 - Shepherd of Fire.flac",
            main.Track("Avenged Sevenfold", "Hail to the King"),
            0,
        ),
        (
            r"music\Fox Capture Plan\10 - Freak on a Leash (Korn).flac",
            main.Track("Korn", "Freak on a Leash"),
            0,
        ),
        (
            r"1986.Master of Puppets\01 - Metallica - Battery.flac",
            main.Track("Metallica", "Master of Puppets"),
            0,
        ),
        (r"Artist\Album\Song.txt", main.Track("Artist", "Song"), 0),
    ],
)
def test_result_confidence(filename, track, confidence):
    assert main.result_confidence({"filename": filename}, track) == confidence


def test_result_confidence_rejects_locked_file():
    file = {"filename": r"Artist\Album\Song.flac", "isLocked": True}
    assert main.result_confidence(file, main.Track("Artist", "Song")) == 0


@pytest.mark.parametrize(
    ("filename", "number"),
    [
        ("1-11 All I Wanted.flac", "11"),
        ("02.12 EARFQUAKE.flac", "12"),
        ("Track 14 - I'm Gone.flac", "14"),
        ("A2 - Midnight City.flac", "02"),
        ("Album - 03 - No Love in LA.flac", "03"),
        ("Royel Otis_Album_01_Oysters.flac", "01"),
    ],
)
def test_extract_track_number(filename, number):
    assert main.extract_track_number(filename) == number


def test_pick_best_prefers_confidence_then_quality():
    track = main.Track("Artist", "Song")
    files = [
        {"filename": r"Artist\Album\Song.flac"},
        {"filename": r"Album\Artist - Song.mp3", "bitRate": 320},
    ]
    assert main.pick_best(files, track) is files[1]


def test_pick_best_prefers_available_peer_at_equal_quality():
    track = main.Track("Artist", "Song")
    queued = {
        "filename": r"Artist\Album\Song.flac",
        "_has_free_slot": False,
        "_queue_length": 20,
        "_upload_speed": 100,
    }
    available = {
        "filename": r"Artist\Other\Song.flac",
        "_has_free_slot": True,
        "_queue_length": 0,
        "_upload_speed": 50,
    }
    assert main.pick_best([queued, available], track) is available


def test_library_index_uses_first_directory_for_tagless_audio(tmp_path):
    cfg = config(tmp_path)
    path = cfg.music_dir / "Artist" / "Album" / "01 - Song.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")

    with patch.object(main, "read_audio_metadata", return_value=(None, None)):
        assert main.build_library_index(cfg) == {("artist", "song"): path}


def test_library_index_does_not_add_path_fallback_when_tagged(tmp_path):
    cfg = config(tmp_path)
    path = cfg.music_dir / "Wrong Path Artist" / "01 - Wrong Title.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")

    with patch.object(
        main,
        "read_audio_metadata",
        return_value=("Tagged Artist", "Tagged Title"),
    ):
        assert main.build_library_index(cfg) == {
            ("tagged artist", "tagged title"): path
        }


def test_library_index_combines_partial_tags_with_path(tmp_path):
    cfg = config(tmp_path)
    path = cfg.music_dir / "Path Artist" / "01 - Path Title.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"audio")

    with patch.object(main, "read_audio_metadata", return_value=("Tagged Artist", None)):
        assert main.build_library_index(cfg) == {("tagged artist", "path title"): path}


def test_library_index_ignores_invalid_audio(tmp_path):
    cfg = config(tmp_path)
    path = cfg.music_dir / "Artist" / "Song.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not audio")

    with patch.object(
        main,
        "read_audio_metadata",
        side_effect=main.InvalidAudio("bad"),
    ):
        assert main.build_library_index(cfg) == {}


def test_read_audio_metadata_distinguishes_corrupt_and_tagless(tmp_path):
    path = tmp_path / "track.flac"
    with patch.object(main, "MutagenFile", return_value=None):
        with pytest.raises(main.InvalidAudio):
            main.read_audio_metadata(path)

    with patch.object(main, "MutagenFile", return_value=SimpleNamespace(tags={})):
        assert main.read_audio_metadata(path) == (None, None)


def test_verify_downloaded_file_checks_tags_and_validity(tmp_path):
    track = main.Track("Artist", "Song")
    with patch.object(main, "read_audio_metadata", return_value=("Artist", "Song")):
        assert main.verify_downloaded_file(tmp_path / "song.flac", track)
    with patch.object(main, "read_audio_metadata", return_value=("Other", "Song")):
        assert not main.verify_downloaded_file(tmp_path / "song.flac", track)
    with patch.object(main, "read_audio_metadata", return_value=("Artist", None)):
        assert main.verify_downloaded_file(tmp_path / "song.flac", track)
    with patch.object(
        main,
        "read_audio_metadata",
        side_effect=main.InvalidAudio("corrupt"),
    ):
        assert not main.verify_downloaded_file(tmp_path / "song.flac", track)


def test_wait_for_download_detects_overwritten_file(tmp_path):
    cfg = config(tmp_path)
    path = cfg.download_dir / "song.flac"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"old")
    before = main.download_snapshot(cfg.download_dir)

    path.write_bytes(b"new audio")
    previous_mtime = before[path][2]
    os.utime(path, ns=(previous_mtime + 1_000_000, previous_mtime + 1_000_000))

    assert main.wait_for_download(
        cfg,
        {"filename": r"share\song.flac", "size": 9},
        before,
    ) == path


def test_wait_for_download_detects_collision_rename(tmp_path):
    cfg = config(tmp_path)
    cfg.download_dir.mkdir(parents=True)
    (cfg.download_dir / "song.flac").write_bytes(b"old")
    before = main.download_snapshot(cfg.download_dir)
    renamed = cfg.download_dir / "song (1).flac"
    renamed.write_bytes(b"new")

    assert main.wait_for_download(
        cfg,
        {"filename": "song.flac", "size": 3},
        before,
    ) == renamed


def test_wait_for_download_detects_slskd_timestamp_rename(tmp_path):
    cfg = config(tmp_path)
    cfg.download_dir.mkdir(parents=True)
    (cfg.download_dir / "song.flac").write_bytes(b"old")
    before = main.download_snapshot(cfg.download_dir)
    renamed = cfg.download_dir / "song_638942123456789012.flac"
    renamed.write_bytes(b"new")

    assert main.wait_for_download(
        cfg,
        {"filename": "song.flac", "size": 3},
        before,
    ) == renamed


def test_import_reports_existing_destination_as_skipped(tmp_path):
    cfg = config(tmp_path)
    source = cfg.download_dir / "01 - Song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"new")
    destination = main.build_destination(cfg, main.Track("Artist", "Song"), source)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"existing")

    assert not main.import_download(cfg, source, main.Track("Artist", "Song"))
    assert source.exists()
    assert destination.read_bytes() == b"existing"


def test_import_moves_download_and_cleans_empty_parents(tmp_path):
    cfg = config(tmp_path)
    source = cfg.download_dir / "nested" / "07 - Song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")

    assert main.import_download(cfg, source, main.Track("Artist", "Song"))
    destination = cfg.music_dir / "Artist" / "07. Song.flac"
    assert destination.read_bytes() == b"audio"
    assert not source.exists()
    assert not source.parent.exists()


def test_import_does_not_publish_partial_copy(tmp_path):
    cfg = config(tmp_path)
    source = cfg.download_dir / "Song.flac"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"audio")
    destination = main.build_destination(cfg, main.Track("Artist", "Song"), source)

    with (
        patch.object(main.shutil, "copyfileobj", side_effect=OSError("copy failed")),
        pytest.raises(OSError, match="copy failed"),
    ):
        main.import_download(cfg, source, main.Track("Artist", "Song"))

    assert source.exists()
    assert not destination.exists()
    assert list(destination.parent.glob(".soulbrainz-*")) == []


def test_run_returns_failure_if_any_track_fails(tmp_path):
    cfg = config(tmp_path)
    weekly = main.WeeklyJams(
        "weekly",
        date(2026, 2, 1),
        (main.Track("A", "one"), main.Track("B", "two")),
    )
    with (
        patch.object(main, "build_library_index", return_value={}),
        patch.object(main, "get_weekly_jams", return_value=weekly),
        patch.object(main, "process_track", side_effect=["imported", "failed"]),
    ):
        assert main.run(cfg, Mock()) == 1


def test_weekly_paths_preserve_order_and_remove_duplicate_files(tmp_path):
    first = tmp_path / "music" / "First.flac"
    second = tmp_path / "music" / "Second.flac"
    weekly = main.WeeklyJams(
        "weekly",
        date(2026, 2, 1),
        (
            main.Track("B", "two"),
            main.Track("A", "one"),
            main.Track("B", "two"),
            main.Track("Missing", "track"),
        ),
    )
    library = {
        main.track_key("A", "one"): first,
        main.track_key("B", "two"): second,
    }

    assert main._weekly_paths(weekly, library) == [second, first]


def test_write_m3u_uses_stable_weekly_path_and_utf8(tmp_path):
    cfg = config(tmp_path, playlist_dir=tmp_path / "playlists")
    weekly = main.WeeklyJams(
        "weekly",
        date(2026, 2, 1),
        (main.Track("Beyoncé", "Halo"),),
    )
    track = cfg.music_dir / "Beyoncé" / "Halo.flac"

    playlist = main.write_m3u(cfg, weekly, [track])

    assert playlist == cfg.playlist_dir / "weekly-jams-2026-02-01.m3u8"
    assert playlist.read_text(encoding="utf-8") == f"#EXTM3U\n{track}\n"
    assert playlist.stat().st_mode & 0o777 == 0o664


def test_publish_plex_playlist_scans_imports_renames_and_verifies(tmp_path):
    cfg = config(
        tmp_path,
        plex_url="http://plex:32400",
        plex_token="token",
        plex_library="Music",
        playlist_dir=tmp_path / "playlists",
        plex_scan_timeout=0.1,
    )
    weekly = main.WeeklyJams(
        "weekly",
        date(2026, 2, 1),
        (main.Track("A", "one"), main.Track("B", "two")),
    )
    paths = [cfg.music_dir / "A.flac", cfg.music_dir / "B.flac"]
    m3u_path = cfg.playlist_dir / weekly.filename
    section = {
        "type": "artist",
        "title": "Music",
        "key": "4",
        "refreshing": False,
    }
    imported = {
        "guid": f"com.plexapp.agents.none://{m3u_path}",
        "key": "/playlists/42/items",
        "title": "weekly-jams-2026-02-01",
        "leafCount": "2",
    }
    http = Mock()
    http.get.side_effect = [
        Response({"MediaContainer": {"Directory": [section]}}),
        Response({"MediaContainer": {"Activity": []}}),
        Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"Media": [{"Part": [{"file": str(paths[0])}]}]},
                        {"Media": [{"Part": [{"file": str(paths[1])}]}]},
                    ]
                }
            }
        ),
        Response({"MediaContainer": {"Metadata": [imported]}}),
        Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"Media": [{"Part": [{"file": str(paths[0])}]}]},
                        {"Media": [{"Part": [{"file": str(paths[1])}]}]},
                    ]
                }
            }
        ),
    ]
    http.post.side_effect = [
        Response({}, headers={"X-Plex-Activity": "scan-activity"}),
        Response({}),
    ]
    http.put.return_value = Response({})

    main.publish_plex_playlist(cfg, http, weekly, paths)

    assert http.post.call_args_list[0].args[0].endswith(
        "/library/sections/4/refresh"
    )
    upload = http.post.call_args_list[1]
    assert upload.args[0].endswith("/playlists/upload")
    assert upload.kwargs["params"] == {"sectionID": "4", "path": str(m3u_path)}
    rename = http.put.call_args
    assert rename.args[0] == "http://plex:32400/playlists/42"
    assert rename.kwargs["params"]["title.value"] == "Weekly Jams — 2026-02-01"
    assert rename.kwargs["headers"]["X-Plex-Token"] == "token"


def test_publish_plex_playlist_rejects_incomplete_import(tmp_path):
    cfg = config(
        tmp_path,
        plex_url="http://plex:32400",
        plex_token="token",
        playlist_dir=tmp_path / "playlists",
        plex_scan_timeout=0,
    )
    weekly = main.WeeklyJams(
        "weekly",
        date(2026, 2, 1),
        (main.Track("A", "one"), main.Track("B", "two")),
    )
    paths = [cfg.music_dir / "A.flac", cfg.music_dir / "B.flac"]
    m3u_path = cfg.playlist_dir / weekly.filename
    section = {
        "type": "artist",
        "title": "Music",
        "key": "4",
        "refreshing": False,
    }
    http = Mock()
    http.get.side_effect = [
        Response({"MediaContainer": {"Directory": [section]}}),
        Response({"MediaContainer": {"Activity": []}}),
        Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"Media": [{"Part": [{"file": str(paths[0])}]}]},
                        {"Media": [{"Part": [{"file": str(paths[1])}]}]},
                    ]
                }
            }
        ),
        Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {
                            "guid": f"file://{m3u_path}",
                            "ratingKey": "42",
                            "title": weekly.title,
                            "leafCount": 1,
                        }
                    ]
                }
            }
        ),
        Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"Media": [{"Part": [{"file": str(paths[0])}]}]}
                    ]
                }
            }
        ),
    ]
    http.post.side_effect = [
        Response({}, headers={"X-Plex-Activity": "scan-activity"}),
        Response({}),
    ]

    with pytest.raises(RuntimeError, match="has 1 items but does not match"):
        main.publish_plex_playlist(cfg, http, weekly, paths)

    http.put.assert_not_called()


def test_publish_plex_playlist_waits_for_same_week_overwrite(tmp_path):
    cfg = config(
        tmp_path,
        plex_url="http://plex:32400",
        plex_token="token",
        playlist_dir=tmp_path / "playlists",
        plex_scan_timeout=0.1,
        poll_interval=0,
    )
    weekly = main.WeeklyJams(
        "weekly",
        date(2026, 2, 1),
        (main.Track("A", "one"), main.Track("B", "two")),
    )
    paths = [cfg.music_dir / "A.flac", cfg.music_dir / "B.flac"]
    old_paths = [cfg.music_dir / "Old A.flac", cfg.music_dir / "Old B.flac"]
    m3u_path = cfg.playlist_dir / weekly.filename
    section = {
        "type": "artist",
        "title": "Music",
        "key": "4",
        "refreshing": False,
    }
    playlist = {
        "guid": f"file://{m3u_path}",
        "ratingKey": "42",
        "title": weekly.title,
        "leafCount": 2,
    }

    def playlist_items(item_paths):
        return Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"Media": [{"Part": [{"file": str(path)}]}]}
                        for path in item_paths
                    ]
                }
            }
        )

    http = Mock()
    http.get.side_effect = [
        Response({"MediaContainer": {"Directory": [section]}}),
        Response({"MediaContainer": {"Activity": []}}),
        playlist_items(paths),
        Response({"MediaContainer": {"Metadata": [playlist]}}),
        playlist_items(old_paths),
        Response({"MediaContainer": {"Metadata": [playlist]}}),
        playlist_items(paths),
    ]
    http.post.side_effect = [
        Response({}, headers={"X-Plex-Activity": "scan-activity"}),
        Response({}),
    ]

    main.publish_plex_playlist(cfg, http, weekly, paths)

    http.put.assert_not_called()
    assert http.get.call_count == 7


def test_refresh_plex_library_waits_for_scan_to_start_and_finish(tmp_path):
    cfg = config(
        tmp_path,
        plex_url="http://plex:32400",
        plex_token="token",
        plex_scan_timeout=1,
        poll_interval=0,
    )
    initial = {
        "type": "artist",
        "title": "Music",
        "key": "4",
        "refreshing": False,
        "scannedAt": 10,
    }
    http = Mock()
    http.post.return_value = Response({})
    http.get.side_effect = [
        Response({"MediaContainer": {"Directory": [{**initial}]}}),
        Response(
            {
                "MediaContainer": {
                    "Directory": [{**initial, "refreshing": True}]
                }
            }
        ),
        Response(
            {
                "MediaContainer": {
                    "Directory": [
                        {**initial, "refreshing": False, "scannedAt": 11}
                    ]
                }
            }
        ),
    ]

    main.refresh_plex_library(cfg, http, initial)

    assert http.get.call_count == 3


def test_wait_for_plex_tracks_waits_for_every_exact_path(tmp_path):
    cfg = config(
        tmp_path,
        plex_url="http://plex:32400",
        plex_token="token",
        plex_scan_timeout=0.1,
        poll_interval=0,
    )
    paths = [cfg.music_dir / "A.flac", cfg.music_dir / "B.flac"]

    def tracks(*available):
        return Response(
            {
                "MediaContainer": {
                    "Metadata": [
                        {"Media": [{"Part": [{"file": str(path)}]}]}
                        for path in available
                    ]
                }
            }
        )

    http = Mock()
    http.get.side_effect = [tracks(paths[0]), tracks(*paths)]

    main.wait_for_plex_tracks(cfg, http, "4", paths)

    assert http.get.call_count == 2
    assert http.get.call_args.kwargs["params"]["type"] == 10


def test_find_plex_playlist_paginates(tmp_path):
    cfg = config(
        tmp_path,
        plex_url="http://plex:32400",
        plex_token="token",
        plex_scan_timeout=0.1,
    )
    m3u_path = tmp_path / "weekly.m3u8"
    wanted = {"guid": f"file://{m3u_path}", "ratingKey": "2"}
    http = Mock()
    http.get.side_effect = [
        Response(
            {
                "MediaContainer": {
                    "size": 1,
                    "totalSize": 2,
                    "Metadata": [{"guid": "file:///other.m3u8"}],
                }
            }
        ),
        Response(
            {
                "MediaContainer": {
                    "size": 1,
                    "totalSize": 2,
                    "Metadata": [wanted],
                }
            }
        ),
    ]

    assert main._find_plex_playlist(cfg, http, m3u_path, "4") == wanted
    assert http.get.call_args_list[0].kwargs["params"]["X-Plex-Container-Start"] == 0
    assert http.get.call_args_list[1].kwargs["params"]["X-Plex-Container-Start"] == 1


def test_run_publishes_available_tracks_in_recommendation_order(tmp_path):
    cfg = config(tmp_path, plex_url="http://plex", plex_token="token")
    tracks = (
        main.Track("B", "two"),
        main.Track("Missing", "track"),
        main.Track("A", "one"),
    )
    weekly = main.WeeklyJams("weekly", date(2026, 2, 1), tracks)
    first = cfg.music_dir / "A.flac"
    second = cfg.music_dir / "B.flac"
    library = {
        main.track_key("A", "one"): first,
        main.track_key("B", "two"): second,
    }

    with (
        patch.object(main, "build_library_index", return_value=library),
        patch.object(main, "get_weekly_jams", return_value=weekly),
        patch.object(main, "process_track", side_effect=["skipped"] * 3),
        patch.object(main, "publish_plex_playlist") as publish,
    ):
        assert main.run(cfg, Mock()) == 0

    assert publish.call_args.args[2:] == (weekly, [second, first])


def test_run_returns_failure_when_plex_publish_fails(tmp_path):
    cfg = config(tmp_path, plex_url="http://plex", plex_token="token")
    track = main.Track("A", "one")
    weekly = main.WeeklyJams("weekly", date(2026, 2, 1), (track,))
    library = {main.track_key("A", "one"): cfg.music_dir / "A.flac"}

    with (
        patch.object(main, "build_library_index", return_value=library),
        patch.object(main, "get_weekly_jams", return_value=weekly),
        patch.object(main, "process_track", return_value="skipped"),
        patch.object(
            main,
            "publish_plex_playlist",
            side_effect=RuntimeError("Plex unavailable"),
        ),
    ):
        assert main.run(cfg, Mock()) == 1


def test_search_flattens_responses_without_mutating_them(tmp_path):
    cfg = config(tmp_path)
    original = {"filename": r"Artist\Song.flac"}
    http = Mock()
    http.post.return_value = Response({})
    http.get.side_effect = [
        Response({"isComplete": True}),
        Response(
            [
                {
                    "username": "peer",
                    "hasFreeUploadSlot": True,
                    "queueLength": 2,
                    "uploadSpeed": 123,
                    "files": [original],
                }
            ]
        ),
    ]

    result = main.search_slskd(cfg, http, main.Track("Artist", "Song"))
    assert result == [
        {
            "filename": original["filename"],
            "_username": "peer",
            "_has_free_slot": True,
            "_queue_length": 2,
            "_upload_speed": 123,
        }
    ]
    assert "_username" not in original


def test_queue_download_sends_api_key_and_escaped_username(tmp_path):
    cfg = config(tmp_path, slskd_api_key="secret")
    http = Mock()
    http.post.return_value = Response({})
    main.queue_download(
        cfg,
        http,
        {"_username": "peer/name", "filename": "song.flac", "size": 12},
    )

    call = http.post.call_args
    assert call.args[0].endswith("/downloads/peer%2Fname")
    assert call.kwargs["headers"]["X-API-Key"] == "secret"
