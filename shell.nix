{ pkgs ? import <nixpkgs> {} }:

pkgs.mkShell {
  packages = with pkgs; [
    docker
    docker-compose
    jq
  ];

  shellHook = ''
    echo "jira-crit-path-finder dev shell"
    echo "  ./bootstrap.sh         — build + serve via Tailscale Funnel"
    echo "  ./bootstrap.sh --stop  — tear down container + disable Funnel"
  '';
}
