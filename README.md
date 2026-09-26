# Soulbrainz

Soulbrainz imports a ListenBrainz user's newest **Weekly Jams** into a local
music library, then publishes a dated playlist to Plex for use in Plexamp. It
skips tracks already present, searches for the rest via slskd, verifies each
download, and keeps earlier weekly playlists intact.

Each run writes a stable file such as
`/data/music/.playlists/weekly-jams-2026-09-21.m3u8`, refreshes the Plex music
library, imports that file, and verifies the number of playlist entries. A rerun
updates the same week; a new ListenBrainz week creates a new Plex playlist. The
default timer checks daily so a late ListenBrainz publication or transient
download failure is repaired without waiting another week.

## Configuration

The executable reads these environment variables:

| Variable | Default |
| --- | --- |
| `LISTENBRAINZ_USER` | `reisdro` |
| `SLSKD_URL` | `https://slskd.voldsoy.duckdns.org` |
| `SLSKD_API_KEY` | unset |
| `MUSIC_DIR` | `/data/music` |
| `DOWNLOAD_DIR` | `/data/downloads/slskd/complete` |
| `PLEX_URL` | unset (publishing disabled) |
| `PLEX_TOKEN` | unset |
| `PLEX_LIBRARY` | `Music` |
| `PLAYLIST_DIR` | `$MUSIC_DIR/.playlists` |

The NixOS module exposes matching options under `services.soulbrainz`, plus the
service user/group, schedule, and package. Plex publishing is enabled by default
for a server at `http://127.0.0.1:32400`. The module securely loads the existing
Plex owner token from its protected preferences file with a systemd credential;
the token is never placed in the Nix store, environment, process arguments, or
logs. Set `plexPreferencesFile = null` and provide `PLEX_TOKEN` through
`environmentFile` when Plex is remote or uses a different account.

The Plex token determines which account owns the resulting playlists, so it
must be the account used in Plexamp. Plex must see the same absolute music paths
written to the M3U8 files. On Nico, both services use `/data/music` and share the
`media` group. Every writer of that tree must use the same group and a `0002`
umask. By default, a privileged pre-start step adds group-write permission to
music directories that are missing it; it never changes file contents or
ownership. Set `repairDirectoryPermissions = false` if permissions are managed
elsewhere.

```nix
{
  services.soulbrainz = {
    enable = true;
    listenbrainzUser = "reisdro";
    onCalendar = "*-*-* 06:00:00";
    plexLibrary = "Music";
  };
}
```

Set `plexUrl = null` to retain download-only behavior. `environmentFile` may
contain `SLSKD_API_KEY` and, when needed, `PLEX_TOKEN`.

## Verification

`nix flake check path:. -L` runs the Python regression suite, imports the built
package, and evaluates the NixOS module for the current system. Checks are
declared for both x86-64 and AArch64 Linux; use `--all-systems --no-build` to
evaluate both without requiring a cross-system builder.
