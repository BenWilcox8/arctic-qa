{
  description = "Arctic QA local development shell";

  inputs.nixpkgs.url = "github:NixOS/nixpkgs/nixos-26.05";

  outputs = { self, nixpkgs }:
    let
      systems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = nixpkgs.lib.genAttrs systems;
    in {
      devShells = forAllSystems (system:
        let pkgs = import nixpkgs { inherit system; };
        in {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python313
              python313Packages.pip
              python313Packages.pytest
              python313Packages.setuptools
              python313Packages.wheel
              poppler-utils
              ruff
            ];
          };
        });
    };
}
