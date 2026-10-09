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
  menu_base=auto; menu_bridge=auto; menu_storage=local-lvm; menu_uplink=auto; menu_images=/var/lib/vz/demo-images
  while (($#)); do
   (($#>=2)) || { echo "Missing value for $1" >&2; exit 2; }
   case "$1" in
    --base-id) menu_base=$2;; --bridge-base) menu_bridge=$2;;
    --storage) menu_storage=$2;; --uplink) menu_uplink=$2;; --image-dir) menu_images=$2;;
    *) echo "Unknown menu option: $1" >&2; exit 2;;
   esac
   shift 2
  done
  menu_prompt() {
   local value
   read -r -p "$1 [$2]: " value || exit 0
   printf -v "$3" '%s' "${value:-$2}"
  }
  while true; do
   menu_args=(--base-id "$menu_base" --bridge-base "$menu_bridge" --storage "$menu_storage" --uplink "$menu_uplink" --image-dir "$menu_images")
   printf '\nСтенд демэкзамена — Proxmox\n'
   printf 'Ideco: 6 ГБ / 4 ядра. EcoRouter: 2 ГБ / 2 ядра каждый.\n'
   printf 'Первый VMID: %s; первый мост: %s (auto — свободный диапазон)\n' "$menu_base" "$menu_bridge"
   printf 'Диски: %s; внешняя сеть ISP: %s\n' "$menu_storage" "$menu_uplink"
   printf '1 — Показать план и ресурсы (без создания ВМ)\n'
   printf '2 — Создать стенд из готовых qcow2\n'
   printf '3 — Настроить VMID, мосты и хранилище\n'
   printf '0 — Выход\n'
   read -r -p 'Выбери пункт: ' choice || exit 0
   case "$choice" in
    1) if ! bash "$self" --deploy-plan "${menu_args[@]}"; then echo 'План не построен. Проверь настройки в пункте 3.'; fi;;
    2) exec bash "$self" --deploy "${menu_args[@]}";;
    3)
     echo 'Enter сохраняет текущее значение. auto выбирает свободный диапазон; можно указать номер вручную.'
     menu_prompt 'Первый VMID (auto или номер, 8 машин подряд)' "$menu_base" menu_base
     menu_prompt 'Номер первого моста (auto или номер, 9 мостов подряд)' "$menu_bridge" menu_bridge
     menu_prompt 'Хранилище дисков' "$menu_storage" menu_storage
     menu_prompt 'Внешний мост ISP (auto или vmbrN)' "$menu_uplink" menu_uplink
     menu_prompt 'Каталог кэша образов' "$menu_images" menu_images
     ;;
    0) exit 0;;
    *) echo 'Введи 1, 2, 3 или 0.';;
   esac
  done
  ;;
 --deploy-plan|--deploy)
  mode=--plan; [[ $1 != --deploy ]] || mode=--apply
  shift
  for arg in "$@"; do
   [[ $arg != --plan && $arg != --apply ]] || { echo 'Choose --deploy-plan or --deploy without extra mode flags'; exit 2; }
  done
  for file in deploy.sh image-manifest.py detect-uplink.py select-ranges.py images.json; do fetch "$file"; done
  bash -n "$tmp/deploy.sh"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp/image-manifest.py"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp/detect-uplink.py"
  python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$tmp/select-ranges.py"
  python3 -m json.tool "$tmp/images.json" >/dev/null
  for file in deploy.sh image-manifest.py detect-uplink.py select-ranges.py images.json; do mv -- "$tmp/$file" "$file"; done
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
