#!/usr/bin/env bash
set -Eeuo pipefail
umask 077
here=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
mode=plan; base=200; bridgebase=201; storage=local-lvm; uplink=vmbr0
usage() { echo 'deploy.sh [--plan|--apply] [--base-id 200] [--bridge-base 201] [--storage local-lvm] [--uplink vmbr0]'; }
fail() { echo "ERROR: $*" >&2; exit 1; }
while (($#)); do
 case "$1" in
  --plan) mode=plan; shift;; --apply) mode=apply; shift;;
  --base-id|--bridge-base|--storage|--uplink)
   (($#>=2)) || fail "Missing value for $1"
   case "$1" in --base-id) base=$2;; --bridge-base) bridgebase=$2;; --storage) storage=$2;; --uplink) uplink=$2;; esac
   shift 2;;
  --help) usage; exit 0;; *) usage; fail "Unknown option $1";;
 esac
done
[[ $base =~ ^[1-9][0-9]*$ && $bridgebase =~ ^[1-9][0-9]*$ ]] || fail 'Numeric IDs required'
((base>=100 && base<=999999900 && bridgebase<=9991)) || fail 'IDs out of range'
[[ $storage =~ ^[A-Za-z][A-Za-z0-9_-]*$ && $uplink =~ ^vmbr[0-9]+$ ]] || fail 'Invalid storage or uplink'
roles=(ISP HQ-RTR HQ-SW HQ-SRV HQ-CLI BR-RTR BR-FW BR-SRV)
ram=(1024 4096 1024 2048 2048 4096 8192 2048)
cores=(1 4 1 1 2 4 4 1)
disks=(20 10 20 20 32 10 160 20)
kinds=(jeos router jeos server cli router ideco server)
br() { echo "vmbr$((bridgebase+$1-1))"; }
nics=("$uplink,$(br 1),$(br 2)" "$(br 8),$(br 1),$(br 3)" "$(br 3),$(br 4),$(br 5)" "$(br 4)" "$(br 5)" "$(br 9),$(br 2),$(br 6)" "$(br 7),$(br 6)" "$(br 7)")
[[ ,$(IFS=,; echo "${nics[*]}"), == *",$uplink,"* ]] || fail 'Invalid uplink'
for n in {1..9}; do [[ $(br "$n") != "$uplink" ]] || fail 'Bridge range overlaps uplink'; done
printf 'NEW LAB: storage=%s uplink=%s\n' "$storage" "$uplink"
printf '%-7s %-8s %-7s %-7s %-7s %s\n' VMID NAME RAM-MB CORES DISK-GB NETWORKS
for i in "${!roles[@]}"; do
 printf '%-7s %-8s %-7s %-7s %-7s %s\n' "$((base+i))" "${roles[i]}" "${ram[i]}" "${cores[i]}" "${disks[i]}" "${nics[i]}"
done
echo 'ALT: automatic installation, sequential, shutdown after first boot.'
echo 'EcoRouter: import qcow2. Ideco: UEFI, manual installation. Existing VMs are never replaced.'
echo 'All 8 VMs need 24 GiB RAM plus host memory; they will NOT all start automatically.'
[[ $mode == apply ]] || { echo 'Plan only; nothing downloaded or changed.'; exit 0; }
[[ $EUID == 0 ]] || fail 'Run in Proxmox Shell as root'
for cmd in qm pvesm pvesh qemu-img python3 curl ifup flock timeout; do
 command -v "$cmd" >/dev/null || fail "Install prerequisites first: apt-get install python3-venv xorriso ifupdown2 (missing $cmd)"
done
exec 9>/run/lock/proxmox-demo-deploy.lock
flock -n 9 || fail 'Another deploy is running'
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
# This release uses local directory storage for source/private installation ISOs.
iso_dir=$(dirname "$(pvesm path local:iso/demo-placeholder.iso)")
[[ -d $iso_dir && -w $iso_dir ]] || fail 'local ISO directory unavailable'
free_kb=$(df -Pk "$iso_dir" | awk 'END {print $4}')
((free_kb>=26000000)) || fail 'Need at least 26 GB free on local for download and remastering'
available_mb=$(awk '/MemAvailable:/ {print int($2/1024)}' /proc/meminfo)
((available_mb>=5120)) || fail 'Free at least 5 GB RAM before sequential installation'
state=$(mktemp -d /root/demo-deploy.XXXXXXXX)
log=$state/deploy.log
exec > >(tee -a "$log") 2>&1
cp -a /etc/network/interfaces "$state/interfaces.before"
echo "Deployment journal: $state; partial results are retained on failure. No automatic deletion."
trap 'code=$?; echo "FAILED ($code) at line $LINENO. Read $log. Existing lab untouched; new lab may be partial."; exit "$code"' ERR
read -r -s -p 'Password for ALT root and admin (not uploaded to GitHub): ' password; echo
[[ ${#password} -ge 8 && $password != *$'\r'* ]] || fail 'Use at least 8 characters'
read -r -s -p 'Repeat password: ' repeat; echo
[[ $password == "$repeat" ]] || fail 'Passwords differ'
printf %s "$password" > "$state/password"
unset password repeat
# Install only tooling from configured distribution repositories; no full upgrade.
if ! command -v xorriso >/dev/null || ! command -v socat >/dev/null || ! dpkg-query -W -f='${Status}' python3-venv 2>/dev/null | grep -q 'install ok installed'; then
 apt-get update
 apt-get install -y python3-venv xorriso socat
fi
# Isolated Python environment; no changes to Proxmox system Python packages.
python3 -m venv "$here/.venv" || fail 'Install python3-venv first'
"$here/.venv/bin/pip" install 'gdown==6.4.2' 'pycdlib==1.21.0'
py=$here/.venv/bin/python
"$py" - "$here/images.json" "$iso_dir" "$state" <<'PY'
import json,sys,os,hashlib
from pathlib import Path
import gdown
root=Path(sys.argv[2]); manifest=json.load(open(sys.argv[1])); state=Path(sys.argv[3])
for kind,item in manifest.items():
 path=root/('demo-source-'+item['file']); stamp=path.with_suffix(path.suffix+'.demo.sha256')
 def digest(p):
  h=hashlib.sha256()
  with p.open('rb') as f:
   for chunk in iter(lambda:f.read(1024*1024),b''):h.update(chunk)
  return h.hexdigest()
 if path.exists():
  if not stamp.exists() or stamp.read_text().strip()!=digest(path):
   sys.exit(f'Cached file unverified: {path}. Move it aside or supply your own verified checksum.')
 else:
  partial=Path(str(path)+'.demo-download')
  if gdown.download(id=item['id'],output=str(partial),quiet=False,resume=True,use_cookies=False) is None:
   sys.exit('Google Drive download failed: '+item['file'])
  if partial.stat().st_size<1024*1024:sys.exit('Unexpectedly small image')
  os.replace(partial,path); stamp.write_text(digest(path)+'\n')
 with (state/'images.tsv').open('a') as f:f.write(f"{kind}\t{path.name}\n")
print('Downloads complete. Checksums are local integrity records, not vendor signatures.')
PY
# Validate all formats before modifying networking/creating guests.
router=$(awk -F '\t' '$1=="router" {print $2}' "$state/images.tsv")
qemu-img info --output=json "$iso_dir/$router" | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.exit(0 if d["format"]=="qcow2" and not d.get("backing-filename") else "ERROR: Router must be a standalone qcow2 without a backing file")' 
"$py" - "$iso_dir" "$here/images.json" <<'PY'
import sys,json,pycdlib
from pathlib import Path
for kind,item in json.load(open(sys.argv[2])).items():
 if kind=='router':continue
 iso=pycdlib.PyCdlib();iso.open(str(Path(sys.argv[1])/('demo-source-'+item['file'])))
 if kind!='ideco':
  for p in ['/syslinux/isolinux.cfg','/Metadata/pkg-groups.tar']:iso.get_record(rr_path=p)
 else:iso.get_record(rr_path='/efiboot.img')
 iso.close()
PY
for n in {1..9}; do
 b=$(br "$n")
 printf '\n# proxmox-demo-lab %s\nauto %s\niface %s inet manual\n    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\n' "$base" "$b" "$b" >> /etc/network/interfaces
 ifup "$b"
done
for i in "${!roles[@]}"; do
 id=$((base+i)); role=${roles[i]}; kind=${kinds[i]}
 qm create "$id" --name "$role" --memory "${ram[i]}" --balloon 0 --cores "${cores[i]}" --sockets 1 --cpu host --ostype l26 --onboot 0 --description "proxmox-demo-lab: hardware and OS only; journal $state" 
 printf '%s\t%s\n' "$id" "$role" >> "$state/created.tsv"
 model=virtio; [[ $kind != router ]] || model=e1000
 IFS=, read -ra bridges <<< "${nics[i]}"
 for n in "${!bridges[@]}"; do qm set "$id" "--net$n" "$model,bridge=${bridges[n]},firewall=0"; done
 if [[ $kind == router ]]; then
  qm importdisk "$id" "$iso_dir/$router" "$storage"
  volume=$(qm config "$id" | sed -n 's/^unused0: //p' | cut -d, -f1)
  [[ -n $volume ]] || fail "Imported disk missing on $id"
  qm set "$id" --sata0 "$volume" --boot order=sata0
  size=$(qemu-img info --output=json "$iso_dir/$router" | python3 -c 'import json,sys;print(json.load(sys.stdin)["virtual-size"])')
  ((size>=10737418240)) || qm resize "$id" sata0 10G
 elif [[ $kind == ideco ]]; then
  image=$(awk -F '\t' '$1=="ideco" {print $2}' "$state/images.tsv")
  qm set "$id" --bios ovmf --machine q35 --efidisk0 "$storage:1,efitype=4m,pre-enrolled-keys=0" --scsihw virtio-scsi-single --scsi0 "$storage:${disks[i]}" --ide2 "local:iso/$image,media=cdrom" --boot 'order=scsi0;ide2' --rng0 source=/dev/urandom
 else
  qm set "$id" --serial0 socket --sata0 "$storage:${disks[i]}" --ide2 none,media=cdrom --boot 'order=sata0;ide2'
 fi
done
for i in "${!roles[@]}"; do
 kind=${kinds[i]}; [[ $kind != router && $kind != ideco ]] || continue
 id=$((base+i)); role=${roles[i]}
 image=$(awk -F '\t' -v k="$kind" '$1==k {print $2}' "$state/images.tsv")
 mkdir "$state/$role"
 private_iso=$iso_dir/demo-private-$id.iso
 [[ ! -e $private_iso ]] || fail "Private ISO path occupied: $private_iso"
 "$py" "$here/prepare-alt.py" "$iso_dir/$image" "$private_iso" "$role" "$state/$role" "$state/password"
 qm set "$id" --ide2 "local:iso/demo-private-$id.iso,media=cdrom"
 install_ram=2048; [[ $kind != cli ]] || install_ram=4096
 qm set "$id" --memory "$install_ram"
 echo "Installing $role ($id). It should power off after its first installed boot."
 qm start "$id"
 timeout 5500 socat -u "UNIX-CONNECT:/run/qemu-server/$id.serial0" STDOUT > "$state/$role/serial.log" &
 serial_pid=$!
 deadline=$((SECONDS+5400))
 sleep 10
 while [[ $(qm status "$id") != 'status: stopped' ]]; do
  ((SECONDS<deadline)) || fail "$role installation timed out; inspect its console. ISO and VM retained."
  sleep 10
 done
 wait "$serial_pid" || true
 grep -q 'DEMO_INSTALL_OK' "$state/$role/serial.log" || fail "$role stopped without installation-success marker. Inspect console and $state/$role/serial.log; ISO retained."
 qm set "$id" --memory "${ram[i]}" --ide2 none,media=cdrom --boot order=sata0
 # Remove only our credential-bearing ISO after it is detached from the VM.
 rm -- "$private_iso"
 echo "$role stopped after installation. Check its first normal boot before using it."
done
rm -- "$state/password"
echo "Finished: VM IDs $base–$((base+7)). All machines are stopped."
echo "Start ALT/EcoRouter as needed; start BR-FW to install Ideco manually."
echo "Journal: $state. Runtime metadata contains the installation password; keep root-only permissions."
