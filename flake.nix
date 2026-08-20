{
  description = "ListenBrainz Weekly Jams -> slskd downloader";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-26.05";

  outputs = {
    self,
    nixpkgs,
  }: let
    system = "x86_64-linux";
    pkgs = nixpkgs.legacyPackages.${system};

    soulbrainz = pkgs.python3Packages.buildPythonApplication {
      pname = "soulbrainz";
      version = "0.1.0";
      src = ./.;
      pyproject = true;
      build-system = [pkgs.python3Packages.setuptools];
      propagatedBuildInputs = [pkgs.python3Packages.requests];
    };
  in {
    packages.${system}.default = soulbrainz;

    nixosModules.default = {
      config,
      lib,
      pkgs,
      ...
    }: let
      cfg = config.services.soulbrainz;
      pkg = self.packages.${pkgs.system}.default;
    in {
      options.services.soulbrainz = {
        enable = lib.mkEnableOption "soulbrainz Weekly Jams downloader";

        user = lib.mkOption {
          type = lib.types.str;
          default = "soulbrainz";
          description = "System user to run the service as.";
        };

        onCalendar = lib.mkOption {
          type = lib.types.str;
          default = "weekly";
          description = "System OnCalendar expression for the timer (e.g. \"weekly\", \"Mon *-*-* 02:00:00\").";
        };
      };

      config = lib.mkIf cfg.enable {
        users.users.${cfg.user} = {
          isSystemUser = true;
          group = cfg.user;
          description = "soulbrainz service user";
        };
        users.groups.${cfg.user} = {};

        systemd.services.soulbrainz = {
          description = "ListenBrainz Weekly Jams -> slskd importer";
          after = ["network-online.target"];
          wants = ["network-online.target"];
          serviceConfig = {
            ExecStart = "${pkg}/bin/soulbrainz";
            Type = "oneshot";
            User = cfg.user;
            StandardOutput = "journal";
            StandardError = "journal";
          };
        };

        systemd.timers.soulbrainz = {
          description = "Run soulbrainz on a schedule";
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
