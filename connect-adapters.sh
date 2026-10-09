#!/usr/bin/env bash
# Configure VM NICs only. No guest, disk, power, or host network mutations.
set -euo pipefail
MODE=plan
BASE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
MAP="$BASE/machines.tsv"
while (($#)); do
  case "$1" in
    --apply) MODE=apply; shift ;;
    --plan) MODE=plan; shift ;;
    --map) [[ $# -ge 2 ]] || exit 2; MAP=$2; shift 2 ;;
    *) echo 'Usage: bash connect-adapters.sh [--plan|--apply] [--map machines.tsv]' >&2; exit 2 ;;
  esac
done
fail() { echo "ERROR: $*" >&2; exit 1; }
[[ $EUID == 0 ]] || fail 'Run in Proxmox node Shell as root.'
command -v qm >/dev/null || fail 'qm is unavailable: this is not a Proxmox node.'
[[ -f $MAP ]] || fail "Missing mapping: $MAP"
command -v flock >/dev/null || fail 'flock is unavailable.'
exec 9>/run/lock/demo-adapters.lock
flock -n 9 || fail 'Another copy is running.'
declare -a ids=() keys=() values=()
declare -A seen=()
changes=0
while read -r role id bridges rest; do
  [[ -n ${role:-} && $role != \#* ]] || continue
  [[ -z ${rest:-} && $id =~ ^[0-9]+$ && $bridges =~ ^vmbr[0-9]+(,vmbr[0-9]+)*$ ]] || fail "Invalid mapping row: $role"
  [[ ! ${seen[$id]+yes} ]] || fail "Duplicate VMID: $id"
  seen[$id]=1
  case "$role" in
    ISP) expected='ISP'; model=virtio ;;
    HQ-RTR|BR-RTR) expected=$role; model=e1000 ;;
    HQ-SW) expected='HQ-SW|HQ-SV'; model=virtio ;;
    HQ-SRV|BR-SRV|HQ-CLI|BR-FW) expected=$role; model=virtio ;;
    *) fail "Unknown role: $role" ;;
  esac
  cfg=$(qm config "$id" --current 1) || fail "Cannot read VM $id"
  name=$(sed -n 's/^name: //p' <<< "$cfg")
  [[ $name =~ ^($expected)$ ]] || fail "VM $id is '$name', expected $expected. Check machines.tsv."
  state=$(qm status "$id")
  if [[ $MODE == apply ]]; then
    [[ $state == 'status: stopped' ]] || fail "VM $id must be shut down first (Suspend is insufficient)."
    ! [[ $cfg =~ (^|$'\n')lock: ]] || fail "VM $id is locked. Resolve its active task first."
    [[ $(qm config "$id") == "$cfg" ]] || fail "VM $id has pending changes. Resolve them before applying."
  fi
  IFS=, read -ra links <<< "$bridges"
  echo "$role (VM $id, $state)"
  for i in "${!links[@]}"; do
    bridge=${links[$i]}
    [[ -d /sys/class/net/$bridge/bridge ]] || fail "Bridge $bridge is missing or inactive. Create an isolated Linux bridge in node Network first."
    key=net$i
    old=$(sed -n "s/^$key: //p" <<< "$cfg")
    if [[ -n $old ]]; then
      value=''
      IFS=, read -ra options <<< "$old"
      for option in "${options[@]}"; do
        case "$option" in bridge=*|tag=*|trunks=*|link_down=*|firewall=*) continue ;; esac
        value+="${value:+,}$option"
      done
      value+=",bridge=$bridge,firewall=0,link_down=0"
    else
      value="$model,bridge=$bridge,firewall=0,link_down=0"
    fi
    echo "  $key -> $bridge (${old:+preserve existing model/MAC; }firewall off, link enabled, no Proxmox VLAN tag)"
    if [[ $old != "$value" ]]; then
      ids+=("$id"); keys+=("$key"); values+=("$value")
      changes=$((changes+1))
    fi
  done
  count=${#links[@]}
  while IFS= read -r extra; do
    n=${extra#net}; n=${n%%:*}
    if ((n >= count)); then echo "  WARNING: extra net$n retained. Review it manually."; fi
  done < <(sed -n '/^net[0-9][0-9]*:/p' <<< "$cfg")
done < "$MAP"
[[ ${#seen[@]} == 8 ]] || fail 'Mapping must contain all eight distinct lab VMs.'
echo "Planned NIC updates: $changes"
if [[ $MODE == plan ]]; then
  echo 'No changes applied. Shut down the lab VMs, then run with --apply.'
  exit 0
fi
if ((changes == 0)); then echo 'Already configured.'; exit 0; fi
backup=$(mktemp -d /root/demo-adapters-backup.XXXXXXXX)
chmod 700 "$backup"
cp -- "$MAP" "$backup/machines.tsv"
for id in "${!seen[@]}"; do
  cp -- "/etc/pve/qemu-server/$id.conf" "$backup/$id.conf"
done
printf '#!/usr/bin/env bash\nset -euo pipefail\n' > "$backup/restore-nics.sh"
for n in "${!ids[@]}"; do
  id=${ids[$n]}; key=${keys[$n]}
  old=$(sed -n "s/^$key: //p" "$backup/$id.conf")
  if [[ -n $old ]]; then
    printf 'qm set %q --%s %q\n' "$id" "$key" "$old" >> "$backup/restore-nics.sh"
  else
    printf 'qm set %q --delete %q\n' "$id" "$key" >> "$backup/restore-nics.sh"
  fi
done
trap 'echo "Stopped on error. Backup: $backup. Changes may be partial; do not start VMs before reviewing." >&2' ERR
for n in "${!ids[@]}"; do
  qm set "${ids[$n]}" "--${keys[$n]}" "${values[$n]}"
done
echo "Done. Backup and NIC rollback script: $backup"
echo 'Guest configurations unchanged. Start and check the VMs manually.'
