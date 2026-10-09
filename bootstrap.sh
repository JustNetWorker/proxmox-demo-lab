#!/usr/bin/env bash
set -euo pipefail
[[ $EUID == 0 ]] || { echo 'Run in Proxmox Shell as root.' >&2; exit 1; }
command -v qm >/dev/null || { echo 'Proxmox node required.' >&2; exit 1; }
self=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/$(basename -- "${BASH_SOURCE[0]}")
base=https://raw.githubusercontent.com/JustNetWorker/proxmox-demo-lab/main
mkdir -p /root/proxmox-demo-lab
cd /root/proxmox-demo-lab
tmp=$(mktemp -d)
trap 'rm -rf -- "$tmp"' EXIT
fetch() { curl --fail --show-error --location "$base/$1" -o "$tmp/$1"; }
case "${1:---plan}" in
 --menu)
  shift
  while true; do
   printf '\nСтенд демэкзамена — Proxmox\n'
   printf 'Ideco: 6 ГБ / 4 ядра. EcoRouter: 2 ГБ / 2 ядра каждый.\n'
   printf '1 — Показать план и ресурсы (без создания ВМ)\n'
   printf '2 — Создать стенд из готовых qcow2\n'
   printf '    Включая Ideco с EFI-диском. ВМ 200–207 по умолчанию.\n'
   printf '0 — Выход\n'
   read -r -p 'Выбери пункт: ' choice || exit 0
   case "$choice" in
    1) bash "$self" --deploy-plan "$@";;
    2) exec bash "$self" --deploy "$@";;
    0) exit 0;;
    *) echo 'Введи 1, 2 или 0.';;
   esac
  done
  ;;
 --deploy-plan|--deploy)
  mode=--plan; [[ $1 != --deploy ]] || mode=--apply
  shift
  for arg in "$@"; do
   [[ $arg != --plan && $arg != --apply ]] || { echo 'Choose --deploy-plan or --deploy without extra mode flags'; exit 2; }
  done
  for file in deploy.sh image-manifest.py detect-uplink.py images.json; do fetch "$file"; done
  bash -n "$tmp/deploy.sh"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp/image-manifest.py"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp/detect-uplink.py"
  python3 -m json.tool "$tmp/images.json" >/dev/null
  for file in deploy.sh image-manifest.py detect-uplink.py images.json; do mv -- "$tmp/$file" "$file"; done
  bash ./deploy.sh "$mode" "$@"
  ;;
 --plan|--apply)
  [[ $# -le 1 ]] || { echo 'Use --deploy-plan or --deploy for a new lab'; exit 2; }
  fetch connect-adapters.sh
  bash -n "$tmp/connect-adapters.sh"
  mv -- "$tmp/connect-adapters.sh" connect-adapters.sh
  if [[ ! -e machines.tsv ]]; then fetch machines.tsv; mv -- "$tmp/machines.tsv" machines.tsv; fi
  bash ./connect-adapters.sh "${1:---plan}"
  ;;
 *) echo 'Usage: bootstrap.sh [--menu|--plan|--apply|--deploy-plan|--deploy]'; exit 2;;
esac
