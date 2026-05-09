{
  description = "xdb - generic FPGA ILA debug toolkit";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
    };
  };

  outputs =
    {
      nixpkgs,
      flake-utils,
      pyproject-nix,
      uv2nix,
      pyproject-build-systems,
      ...
    }:
    flake-utils.lib.eachDefaultSystem (
      system:
      let
        lib = nixpkgs.lib;
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python3;

        source = builtins.path {
          name = "xdb-source";
          path = ./.;
          filter =
            path: type:
            let
              base = baseNameOf path;
              ignoredDirs = [
                ".git"
                ".direnv"
                ".venv"
                "venv"
                "__pycache__"
                ".pytest_cache"
                ".ruff_cache"
                ".mypy_cache"
                ".pyright"
                ".Xil"
                "xsim.dir"
                "build"
                "dist"
                "htmlcov"
              ];
              ignoredFiles = [
                ".coverage"
                "coverage.xml"
              ];
            in
            !(builtins.elem base ignoredDirs)
            && !(builtins.elem base ignoredFiles)
            && !(type == "symlink" && lib.hasPrefix "result" base)
            && !(lib.hasSuffix ".pyc" base)
            && !(lib.hasSuffix ".pyo" base)
            && !(lib.hasSuffix ".egg-info" base)
            && !(lib.hasSuffix ".jou" base)
            && !(lib.hasSuffix ".log" base)
            && !(lib.hasSuffix ".str" base)
            && !(lib.hasSuffix ".wdb" base)
            && !(lib.hasSuffix ".vcd" base)
            && !(lib.hasSuffix ".fst" base);
        };

        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
        overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
        editableOverlay = workspace.mkEditablePyprojectOverlay {
          root = "$REPO_ROOT";
        };

        pyprojectOverrides = _self: super: {
          xdb = super.xdb.overrideAttrs (_old: {
            src = source;
          });
        };

        pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]
        );
        pythonSetEditable = pythonSet.overrideScope editableOverlay;
        virtualenv = pythonSetEditable.mkVirtualEnv "xdb-dev-env" workspace.deps.all;

        mkCheck =
          name: command: nativeBuildInputs:
          pkgs.stdenvNoCC.mkDerivation {
            pname = "xdb-${name}";
            version = "0.1.0";
            src = source;
            inherit nativeBuildInputs;
            dontConfigure = true;
            dontFixup = true;
            buildPhase = ''
              runHook preBuild
              cp -r $src source
              chmod -R +w source
              cd source
              export HOME=$TMPDIR
              export PYTHONPATH=$PWD/src
              ${command}
              runHook postBuild
            '';
            installPhase = ''
              mkdir -p $out
            '';
          };

        # Runtime variants
        depsUltrascale = workspace.deps.default;
        versalEnabled = builtins.elem "versal" (workspace.deps.optionals.xdb or [ ]);
        depsVersal = workspace.deps.default // {
          xdb = workspace.deps.default.xdb ++ lib.optional versalEnabled "versal";
        };

        ultrascaleEnv = pythonSet.mkVirtualEnv "xdb-ultrascale-env" depsUltrascale;
        versalEnv = pythonSet.mkVirtualEnv "xdb-versal-env" depsVersal;
      in
      {
        packages = rec {
          default = xdb;

          xdb = ultrascaleEnv;
          xdb-ultrascale = ultrascaleEnv;
          xdb-versal = versalEnv;
        };

        apps = rec {
          default = xdb;

          xdb = {
            type = "app";
            program = "${ultrascaleEnv}/bin/xdb";
            meta.description = "Run xdb with the default Ultrascale simulation/debug dependencies";
          };
          xdb-ultrascale = {
            type = "app";
            program = "${ultrascaleEnv}/bin/xdb";
            meta.description = "Run xdb with Ultrascale simulation/debug dependencies";
          };
          xdb-versal = {
            type = "app";
            program = "${versalEnv}/bin/xdb";
            meta.description = "Run xdb with Versal simulation/debug dependencies";
          };
        };

        checks = {
          ruff = mkCheck "ruff" "${pkgs.ruff}/bin/ruff check ." [ pkgs.ruff ];
          pyright = mkCheck "pyright" "${pkgs.pyright}/bin/pyright" [ pkgs.pyright ];
          tests = mkCheck
            "tests"
            "${virtualenv}/bin/python -m unittest discover -s tests"
            [ virtualenv ];
        };

        devShells.default = pkgs.mkShell {
          packages = [
            virtualenv
            pkgs.uv
            pkgs.ruff
            pkgs.pyright
          ];
          env = {
            UV_NO_SYNC = "1";
            UV_PYTHON = pythonSetEditable.python.interpreter;
            UV_PYTHON_DOWNLOADS = "never";
          };
          shellHook = ''
            unset PYTHONPATH
            export REPO_ROOT=$(git rev-parse --show-toplevel)
          '';
        };
      }
    );
}
