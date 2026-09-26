{
  description = "ListenBrainz Weekly Jams downloader and Plex playlist publisher";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs = {
    self,
    nixpkgs,
  }: let
    lib = nixpkgs.lib;
    systems = ["x86_64-linux" "aarch64-linux"];
    forAllSystems = lib.genAttrs systems;

    mkPackage = pkgs:
      pkgs.python3Packages.buildPythonApplication {
        pname = "soulbrainz";
        version = "0.1.0";
        src = ./.;
        pyproject = true;

        build-system = [pkgs.python3Packages.setuptools];
        dependencies = with pkgs.python3Packages; [mutagen requests];
        nativeCheckInputs = [pkgs.python3Packages.pytestCheckHook];
        pythonImportsCheck = ["soulbrainz"];
      };
  in {
    packages = forAllSystems (system: {
      default = mkPackage nixpkgs.legacyPackages.${system};
    });

    checks = forAllSystems (
      system: let
        pkgs = nixpkgs.legacyPackages.${system};
        evaluated = lib.nixosSystem {
          inherit system;
          modules = [
            self.nixosModules.default
            {
              services.soulbrainz.enable = true;
              system.stateVersion = "26.05";
            }
          ];
        };
        service = evaluated.config.systemd.services.soulbrainz;
        timer = evaluated.config.systemd.timers.soulbrainz;
        configured = lib.nixosSystem {
          inherit system;
          modules = [
            self.nixosModules.default
            {
              services.soulbrainz = {
                enable = true;
                playlistDirectory = "/mnt/playlists";
                plexUrl = null;
                plexPreferencesFile = null;
              };
              system.stateVersion = "26.05";
            }
          ];
        };
        configuredService = configured.config.systemd.services.soulbrainz;
      in {
        package = self.packages.${system}.default;
        module =
          assert service.serviceConfig.Group == "media";
          assert service.serviceConfig.UMask == "0002";
          assert lib.hasPrefix "+" service.serviceConfig.ExecStartPre;
          assert service.serviceConfig.LoadCredential == [
            "plex-preferences:/var/lib/plex/Plex Media Server/Preferences.xml"
          ];
          assert service.unitConfig.RequiresMountsFor == [
            "/data/music"
            "/data/downloads/slskd/complete"
          ];
          assert service.environment.LISTENBRAINZ_USER == "reisdro";
          assert service.environment.PLEX_URL == "http://127.0.0.1:32400";
          assert service.environment.PLEX_LIBRARY == "Music";
          assert service.environment.PLAYLIST_DIR == "/data/music/.playlists";
          assert lib.elem "plex.service" service.wants;
          assert timer.timerConfig.Persistent;
          assert timer.timerConfig.OnCalendar == "*-*-* 06:00:00";
          assert !(configuredService.environment ? PLEX_URL);
          assert !(configuredService.serviceConfig ? LoadCredential);
          assert lib.elem "/mnt/playlists" configuredService.unitConfig.RequiresMountsFor;
            pkgs.runCommand "soulbrainz-module-check" {} "touch $out";
      }
    );

    nixosModules.default = {
      config,
      lib,
      pkgs,
      ...
    }: let
      cfg = config.services.soulbrainz;
      playlistDirectory =
        if cfg.playlistDirectory == null
        then "${cfg.musicDirectory}/.playlists"
        else cfg.playlistDirectory;
      localPlex = cfg.plexUrl != null && cfg.plexPreferencesFile != null;
      fixDirectoryPermissions = pkgs.writeShellScript "soulbrainz-fix-directory-permissions" ''
        ${lib.getExe' pkgs.findutils "find"} ${lib.escapeShellArg cfg.musicDirectory} \
          -type d ! -perm -g+w \
          -exec ${lib.getExe' pkgs.coreutils "chmod"} g+w {} +
      '';
    in {
      options.services.soulbrainz = {
        enable = lib.mkEnableOption "the Soulbrainz Weekly Jams importer";

        package = lib.mkOption {
          type = lib.types.package;
          default = mkPackage pkgs;
          defaultText = lib.literalExpression "the package provided by this module";
          description = "Soulbrainz package to run.";
        };

        user = lib.mkOption {
          type = lib.types.str;
          default = "soulbrainz";
          description = "System user that runs Soulbrainz.";
        };

        group = lib.mkOption {
          type = lib.types.str;
          default = "media";
          description = "Shared group for the music and download directories.";
        };

        listenbrainzUser = lib.mkOption {
          type = lib.types.str;
          default = "reisdro";
          description = "ListenBrainz user whose Weekly Jams playlist is imported.";
        };

        slskdUrl = lib.mkOption {
          type = lib.types.str;
          default = "https://slskd.voldsoy.duckdns.org";
          description = "Base URL of the slskd API.";
        };

        musicDirectory = lib.mkOption {
          type = lib.types.str;
          default = "/data/music";
          description = "Music library directory.";
        };

        downloadDirectory = lib.mkOption {
          type = lib.types.str;
          default = "/data/downloads/slskd/complete";
          description = "slskd completed-download directory.";
        };

        playlistDirectory = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = "/data/music/.playlists";
          description = "Directory for stable weekly M3U8 files; defaults to .playlists inside the music directory.";
        };

        plexUrl = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = "http://127.0.0.1:32400";
          description = "Plex Media Server URL, or null to disable Plex playlist publishing.";
        };

        plexLibrary = lib.mkOption {
          type = lib.types.str;
          default = "Music";
          description = "Exact name of the Plex music library.";
        };

        plexPreferencesFile = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = "/var/lib/plex/Plex Media Server/Preferences.xml";
          description = "Plex preferences file passed as a protected systemd credential to obtain the owner token; set null when supplying PLEX_TOKEN another way.";
        };

        repairDirectoryPermissions = lib.mkOption {
          type = lib.types.bool;
          default = true;
          description = "Add group-write permission to music-library directories before each run so members of the shared media group can import tracks.";
        };

        environmentFile = lib.mkOption {
          type = lib.types.nullOr lib.types.str;
          default = null;
          example = "/run/credentials/soulbrainz.env";
          description = "Optional file containing SLSKD_API_KEY or PLEX_TOKEN without copying them to the Nix store.";
        };

        onCalendar = lib.mkOption {
          type = lib.types.str;
          default = "*-*-* 06:00:00";
          description = "systemd OnCalendar expression.";
        };
      };

      config = lib.mkIf cfg.enable {
        users.groups.${cfg.group} = {};
        users.users.${cfg.user} = {
          isSystemUser = true;
          group = cfg.group;
          description = "Soulbrainz service user";
        };

        systemd.services.soulbrainz = {
          description = "Import ListenBrainz Weekly Jams and publish them to Plex";
          after = ["network-online.target"] ++ lib.optional localPlex "plex.service";
          wants = ["network-online.target"] ++ lib.optional localPlex "plex.service";
          unitConfig.RequiresMountsFor =
            [
              cfg.musicDirectory
              cfg.downloadDirectory
            ]
            ++ lib.optional
            (!lib.hasPrefix "${cfg.musicDirectory}/" playlistDirectory)
            playlistDirectory;
          environment = {
            LISTENBRAINZ_USER = cfg.listenbrainzUser;
            SLSKD_URL = cfg.slskdUrl;
            MUSIC_DIR = cfg.musicDirectory;
            DOWNLOAD_DIR = cfg.downloadDirectory;
            PLEX_LIBRARY = cfg.plexLibrary;
            PLAYLIST_DIR = playlistDirectory;
          }
          // lib.optionalAttrs (cfg.plexUrl != null) {
            PLEX_URL = cfg.plexUrl;
          };
          serviceConfig = {
            Type = "oneshot";
            User = cfg.user;
            Group = cfg.group;
            UMask = "0002";
            ExecStart = "${cfg.package}/bin/soulbrainz";
          }
          // lib.optionalAttrs cfg.repairDirectoryPermissions {
            ExecStartPre = "+${fixDirectoryPermissions}";
          }
          // lib.optionalAttrs (cfg.plexUrl != null && cfg.plexPreferencesFile != null) {
            LoadCredential = ["plex-preferences:${cfg.plexPreferencesFile}"];
          }
          // lib.optionalAttrs (cfg.environmentFile != null) {
            EnvironmentFile = cfg.environmentFile;
          };
        };

        systemd.timers.soulbrainz = {
          description = "Run Soulbrainz on a schedule";
          wantedBy = ["timers.target"];
          timerConfig = {
            OnCalendar = cfg.onCalendar;
            Persistent = true;
          };
        };
      };
    };
  };
}
