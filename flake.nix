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

        workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
        overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
        editableOverlay = workspace.mkEditablePyprojectOverlay {
          root = "$REPO_ROOT";
        };

        pyprojectOverrides = _self: _super: { };

        pythonSet = (pkgs.callPackage pyproject-nix.build.packages { inherit python; }).overrideScope (
          lib.composeManyExtensions [
            pyproject-build-systems.overlays.default
            overlay
            pyprojectOverrides
          ]
        );
        pythonSetEditable = pythonSet.overrideScope editableOverlay;
        virtualenv = pythonSetEditable.mkVirtualEnv "xdb-dev-env" workspace.deps.all;
      in
      {
        packages.default = pythonSet.mkVirtualEnv "xdb-env" workspace.deps.default;

        apps.default = {
          type = "app";
          program = "${pythonSet.mkVirtualEnv "xdb-env" workspace.deps.default}/bin/xdb";
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
