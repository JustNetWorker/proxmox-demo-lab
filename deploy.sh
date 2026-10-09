#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mode=plan; base=auto; bridgebase=auto; storage=local-lvm; uplink=auto; image_dir=/var/lib/vz/demo-images
usage() { echo 'deploy.sh [--plan|--apply] [--base-id auto|200] [--bridge-base auto|201] [--storage local-lvm] [--uplink auto|vmbrN] [--image-dir /var/lib/vz/demo-images]'; }
fail() { echo "ERROR: $*" >&2; exit 1; }
while (($#)); do
 case "$1" in
  --plan) mode=plan; shift;; --apply) mode=apply; shift;;
  --base-id|--bridge-base|--storage|--uplink|--image-dir)
   (($#>=2)) || fail "Missing value for $1"
   case "$1" in --base-id) base=$2;; --bridge-base) bridgebase=$2;; --storage) storage=$2;; --uplink) uplink=$2;; --image-dir) image_dir=$2;; esac
   shift 2;;
  --help) usage; exit 0;; *) usage; fail "Unknown option $1";;
 esac
done
[[ ( $base == auto || $base =~ ^[1-9][0-9]{0,8}$ ) && ( $bridgebase == auto || $bridgebase =~ ^[1-9][0-9]{0,3}$ ) ]] || fail 'Numeric IDs required'
[[ $base == auto ]] || ((base>=100 && base<=999999992)) || fail 'VMIDs out of range'
[[ $bridgebase == auto ]] || ((bridgebase<=9991)) || fail 'Bridge IDs out of range'
[[ $storage =~ ^[A-Za-z][A-Za-z0-9_-]*$ && ( $uplink == auto || $uplink =~ ^vmbr[0-9]+$ ) ]] || fail 'Invalid storage or uplink'
if [[ $mode == apply ]]; then
[[ $EUID == 0 ]] || fail 'Run in Proxmox Shell as root'
for cmd in qm pvesm pvesh qemu-img python3 curl ifup flock; do
 command -v "$cmd" >/dev/null || fail "Install prerequisites first: apt-get install python3-venv ifupdown2 (missing $cmd)"
done
exec 9>/run/lock/proxmox-demo-deploy.lock
flock -n 9 || fail 'Another deploy is running'
fi
if [[ $base == auto || $bridgebase == auto ]]; then
 selected=$(python3 "$here/select-ranges.py" "$base" "$bridgebase") || fail 'Cannot choose free VMID/bridge ranges'
 read -r base bridgebase <<< "$selected"
 echo "Selected free ranges: VMIDs ${base}–$((base+7)); bridges vmbr${bridgebase}–vmbr$((bridgebase+8))"
fi
if [[ $uplink == auto ]]; then
 command -v ip >/dev/null || fail 'iproute2 is required for automatic uplink detection'
 uplink=$(python3 "$here/detect-uplink.py") || fail 'Choose a bridge explicitly with --uplink vmbrN'
 [[ -d /sys/class/net/$uplink/bridge ]] || fail "Detected uplink $uplink is not an active Linux bridge"
 echo "Detected host IPv4 uplink: $uplink -> ISP net0"
fi
roles=(ISP HQ-RTR HQ-SW HQ-SRV HQ-CLI BR-RTR BR-FW BR-SRV)
ram=(1024 2048 1024 2048 2048 2048 6144 2048)
cores=(1 2 1 1 2 2 4 1)
disks=(32 10 32 32 32 10 160 32)
kinds=(jeos router jeos server cli router ideco server)
br() { echo "vmbr$((bridgebase+$1-1))"; }
nics=("$uplink,$(br 1),$(br 2)" "$(br 8),$(br 1),$(br 3)" "$(br 3),$(br 4),$(br 5)" "$(br 4)" "$(br 5)" "$(br 9),$(br 2),$(br 6)" "$(br 7),$(br 6)" "$(br 7)")
[[ ,$(IFS=,; echo "${nics[*]}"), == *",$uplink,"* ]] || fail 'Invalid uplink'
for n in {1..9}; do [[ $(br "$n") != "$uplink" ]] || fail 'Bridge range overlaps uplink'; done
printf 'NEW LAB: storage=%s uplink=%s\n' "$storage" "$uplink"
printf '%-7s %-24s %-7s %-7s %-7s %s\n' VMID NAME RAM-MB CORES DISK-GB NETWORKS
for i in "${!roles[@]}"; do
 printf '%-7s %-24s %-7s %-7s %-7s %s\n' "$((base+i))" "${roles[i]}" "${ram[i]}" "${cores[i]}" "${disks[i]}" "${nics[i]}"
done
echo 'All operating systems: import installed standalone qcow2 images; no ISO installation.'
echo 'ALT: SeaBIOS / VirtIO SCSI. Ideco: q35 / UEFI with the exported EFI disk.'
echo 'VM names and hardware are configured; guest passwords and module settings come from your images.'
echo 'ISP net0 uses the existing uplink bridge. Host IP/gateway are NOT copied; configure DHCP or a unique static address inside ISP.'
echo 'All 8 VMs need 18 GiB RAM plus host memory; they will NOT all start automatically.'
[[ $mode == apply ]] || { echo 'Plan only; nothing downloaded or changed.'; exit 0; }
[[ -d /sys/class/net/$uplink/bridge ]] || fail "Uplink $uplink is not an active bridge"
[[ ! -e /etc/network/interfaces.new ]] || fail 'Apply or discard pending network changes first'
[[ -f /etc/network/interfaces && ! -L /etc/network/interfaces ]] || fail 'Unsupported network configuration'
# Full preflight before any downloads or changes, including IDs on other cluster nodes/LXC.
resources=$(pvesh get /cluster/resources --type vm --output-format json)
python3 - "$base" "$resources" <<'PY'
import json,sys
ids={int(x['vmid']) for x in json.loads(sys.argv[2])}
wanted=set(range(int(sys.argv[1]),int(sys.argv[1])+8))
if ids&wanted:sys.exit('ERROR: IDs already occupied: '+str(sorted(ids&wanted)))
PY
for n in {1..9}; do
 b=$(br "$n")
 [[ ! -e /sys/class/net/$b ]] || fail "$b already exists; choose a free --bridge-base"
 if grep -Eq "(^|[[:space:]])${b}([[:space:]]|$)" /etc/network/interfaces; then fail "$b already configured"; fi
 if [[ -d /etc/network/interfaces.d ]] && grep -REq "(^|[[:space:]])${b}([[:space:]]|$)" /etc/network/interfaces.d; then fail "$b already configured"; fi
done
pvesm status --storage "$storage" | awk 'NR>1 && $3=="active" {ok=1} END {exit !ok}' || fail 'Disk storage unavailable'
# Validate the manifest before downloading or changing the node. Missing uploads fail here.
[[ $image_dir == /* && $image_dir != / ]] || fail 'image-dir must be an absolute directory other than /'
python3 "$here/image-manifest.py" validate "$here/images.json"
mkdir -p -- "$image_dir"
[[ -w $image_dir ]] || fail 'Image cache is not writable'
state=$(mktemp -d /root/demo-deploy.XXXXXXXX)
log=$state/deploy.log
exec > >(tee -a "$log") 2>&1
cp -a /etc/network/interfaces "$state/interfaces.before"
echo "Deployment journal: $state; partial results are retained on failure."
trap 'code=$?; echo "FAILED ($code) at line $LINENO. Read $log. New lab may be partial."; exit "$code"' ERR
if ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed'; then
 apt-get update
 apt-get install -y python3-venv
fi
python3 -m venv "$here/.venv"
"$here/.venv/bin/pip" install 'gdown==6.4.2'
"$here/.venv/bin/python" "$here/image-manifest.py" download "$here/images.json" "$image_dir" "$state"
# Format, backing-chain and integrity checks complete before bridges or VMs are created.
python3 "$here/image-manifest.py" inspect "$here/images.json" "$image_dir" "$state"
storage_row=$(pvesm status --storage "$storage" | awk 'NR>1 && $3=="active" {print $2, $6}')
read -r storage_type available_kb <<< "$storage_row"
[[ $available_kb =~ ^[0-9]+$ ]] || fail 'Cannot determine available disk storage capacity'
capacity=$(python3 "$here/image-manifest.py" capacity "$state/verified.json" "$storage_type")
printf '%s\n' "$capacity" > "$state/capacity.json"
required_kb=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["required_kb"])' "$capacity")
python3 - "$capacity" "$available_kb" "$storage_type" <<'PYC'
import json,sys
x=json.loads(sys.argv[1]); g=1024**3
print('Storage %s: %.1f GiB available; estimated import %.1f GiB + %.1f GiB reserve; virtual capacity %.1f GiB' %
      (sys.argv[3],int(sys.argv[2])*1024/g,x['initial_bytes']/g,x['reserve_bytes']/g,x['virtual_bytes']/g))
PYC
((available_kb>=required_kb)) || fail "Not enough disk space: need $required_kb KiB including reserve, available $available_kb KiB. Expand the target storage or use --storage with a larger pool."
if [[ $storage_type == lvmthin ]]; then
 echo 'Thin provisioning: pool usage grows as guests write data. Monitor Data% and free space.'
fi
for n in {1..9}; do
 b=$(br "$n")
 printf '\n# proxmox-demo-lab %s\nauto %s\niface %s inet manual\n    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\n' "$base" "$b" "$b" >> /etc/network/interfaces
 ifup "$b"
done
image_path() { python3 - "$state/verified.json" "$1" <<'PYC'
import json,sys
print(json.load(open(sys.argv[1]))[sys.argv[2]]['path'])
PYC
}
import_volume() {
 qm importdisk "$1" "$2" "$storage" >&2
 local v
 v=$(qm config "$1" | sed -n 's/^unused0: //p' | cut -d, -f1)
 [[ -n $v ]] || fail "Imported disk missing on $1"
 printf '%s' "$v"
}
for i in "${!roles[@]}"; do
 id=$((base+i)); role=${roles[i]}; kind=${kinds[i]}; vm_name=$role
 qm create "$id" --name "$vm_name" --memory "${ram[i]}" --balloon 0 --cores "${cores[i]}" --sockets 1 --cpu host --ostype l26 --onboot 0 --description "proxmox-demo-lab: imported OS and hardware; journal $state"
 printf '%s\t%s\n' "$id" "$vm_name" >> "$state/created.tsv"
 model=virtio; [[ $kind != router ]] || model=e1000
 IFS=, read -ra bridges <<< "${nics[i]}"
 for n in "${!bridges[@]}"; do qm set "$id" "--net$n" "$model,bridge=${bridges[n]},firewall=0"; done
 image=$(image_path "$kind")
 volume=$(import_volume "$id" "$image")
 if [[ $kind == router ]]; then
  qm set "$id" --sata0 "$volume" --boot order=sata0
  size=$(qemu-img info --output=json "$image" | python3 -c 'import json,sys;print(json.load(sys.stdin)["virtual-size"])')
  ((size>=10737418240)) || qm resize "$id" sata0 10G
 elif [[ $kind == ideco ]]; then
  qm set "$id" --bios ovmf --machine q35 --scsihw virtio-scsi-single --scsi0 "$volume,iothread=1" --boot order=scsi0
  efivolume=$(import_volume "$id" "$(image_path ideco_efi)")
  qm set "$id" --efidisk0 "$efivolume,efitype=4m,pre-enrolled-keys=0"
  # Preserve only known device identity fields; never execute the exported config.
  identity=()
  while IFS= read -r field; do identity+=("$field"); done < <(python3 "$here/image-manifest.py" identity "$(image_path ideco_config)")
  if [[ -n ${identity[0]:-} ]]; then qm set "$id" --smbios1 "uuid=${identity[0]}"; fi
  if [[ -n ${identity[1]:-} ]]; then qm set "$id" --net0 "virtio=${identity[1]},bridge=${bridges[0]},firewall=0"; fi
 else
  qm set "$id" --bios seabios --scsihw virtio-scsi-single --scsi0 "$volume,iothread=1" --serial0 socket --boot order=scsi0
 fi
done
echo "Finished: VM IDs ${base}–$((base+7)). All machines are stopped."
echo 'Guest hostnames, IP addresses, credentials and exam-module services are NOT changed by this hardware importer.'
echo 'Ideco licensing must be checked in the restored VM; its original EFI and identity were preserved.'
echo "Journal: $state"
