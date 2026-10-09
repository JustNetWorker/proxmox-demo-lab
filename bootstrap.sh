#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run in Proxmox Shell as root.' >&2; exit 1; }
command -v qm >/dev/null || { echo 'Proxmox node required.' >&2; exit 1; }
[[ $# -le 1 && ${1:---plan} =~ ^--(plan|apply)$ ]] || { echo 'Usage: bootstrap.sh [--plan|--apply]' >&2; exit 2; }
base=https://raw.githubusercontent.com/JustNetWorker/proxmox-demo-lab/main
mkdir -p /root/proxmox-demo-lab
cd /root/proxmox-demo-lab
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
curl --fail --show-error --location "$base/connect-adapters.sh" -o "$tmp/connect-adapters.sh"
bash -n "$tmp/connect-adapters.sh"
mv -- "$tmp/connect-adapters.sh" connect-adapters.sh
if [[ ! -e machines.tsv ]]; then
  curl --fail --show-error --location "$base/machines.tsv" -o "$tmp/machines.tsv"
  mv -- "$tmp/machines.tsv" machines.tsv
fi
bash ./connect-adapters.sh "${1:---plan}"
