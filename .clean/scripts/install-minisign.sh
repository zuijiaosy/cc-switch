#!/usr/bin/env bash
# Official static release, pinned so Ubuntu 22.04 does not depend on a missing
# distro package. Supports both GitHub's Linux runner and the download host.
set -euo pipefail

if [[ $# -ne 1 || -z "$1" || "$1" != /* ]]; then
  printf 'Usage: bash install-minisign.sh /absolute/install/directory\n' >&2
  exit 2
fi

minisign_version='0.12'
minisign_sha256='9a599b48ba6eb7b1e80f12f36b94ceca7c00b7a5173c95c3efc88d9822957e73'
minisign_bin_dir="$1"
case "$(uname -s)/$(uname -m)" in
  Linux/x86_64) minisign_arch='x86_64' ;;
  Linux/aarch64|Linux/arm64) minisign_arch='aarch64' ;;
  *) printf 'This installer supports Linux x86_64 and aarch64 only.\n' >&2; exit 2 ;;
esac

minisign_work_dir="$(mktemp -d)"
trap 'rm -rf -- "$minisign_work_dir"' EXIT
curl --fail --show-error --silent --location --retry 4 --retry-all-errors \
  --connect-timeout 15 --max-time 120 --proto '=https' --proto-redir '=https' \
  --output "$minisign_work_dir/minisign.tar.gz" \
  "https://github.com/jedisct1/minisign/releases/download/$minisign_version/minisign-$minisign_version-linux.tar.gz"
printf '%s  %s\n' "$minisign_sha256" "$minisign_work_dir/minisign.tar.gz" | sha256sum --check
tar -xzf "$minisign_work_dir/minisign.tar.gz" --no-same-owner --no-same-permissions --warning=no-unknown-keyword \
  -C "$minisign_work_dir" "minisign-linux/$minisign_arch/minisign"
install -d -m 0755 "$minisign_bin_dir"
install -m 0755 "$minisign_work_dir/minisign-linux/$minisign_arch/minisign" "$minisign_bin_dir/minisign"
"$minisign_bin_dir/minisign" -v

if [[ -n "${GITHUB_PATH:-}" ]]; then
  printf '%s\n' "$minisign_bin_dir" >> "$GITHUB_PATH"
fi
