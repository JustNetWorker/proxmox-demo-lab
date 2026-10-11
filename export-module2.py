#!/usr/bin/env python3
"""Export a stopped, configured lab without modifying guest disks."""
import argparse
import fcntl
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

spec = importlib.util.spec_from_file_location('hardware', Path(__file__).with_name('hardware-profile.py'))
hardware = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hardware)
ROLES = list(hardware.LINKS)

def run(*args):
    return subprocess.check_output([str(x) for x in args], text=True).strip()

def digest(p):
    h = hashlib.sha256()
    with p.open('rb') as f:
        for data in iter(lambda:f.read(4*1024**2), b''):
            h.update(data)
    return h.hexdigest()

def save(p, data):
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2)+'\n')

def main():
    parser = argparse.ArgumentParser(description='Export all eight stopped VMs as standalone qcow2 with hardware metadata')
    parser.add_argument('--ids', required=True, help='ISP,HQ-RTR,HQ-SW,HQ-SRV,HQ-CLI,BR-RTR,BR-FW,BR-SRV IDs')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    ids = args.ids.split(',')
    if len(ids) != 8 or len(set(ids)) != 8 or any(not re.fullmatch(r'[1-9][0-9]{2,8}', x) for x in ids):
        raise ValueError('Supply eight distinct VMIDs in the documented role order')
    root = Path(args.output)
    if not root.is_absolute() or root.exists():
        raise ValueError('Output must be a NEW absolute directory')
    if os.geteuid() != 0:
        raise ValueError('Run as root on Proxmox')
    os.umask(0o077)
    lock = open('/run/lock/proxmox-demo-deploy.lock', 'w')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    configs = {}
    for role, vmid in zip(ROLES, ids):
        text = run('qm', 'config', vmid, '--current', '1')
        if text != run('qm', 'config', vmid):
            raise ValueError(f'VM {vmid}: pending configuration changes')
        cfg = dict(x.split(': ', 1) for x in text.splitlines() if ': ' in x)
        if cfg.get('lock') or run('qm', 'status', vmid) != 'status: stopped':
            raise ValueError(f'VM {vmid}: must be stopped and unlocked')
        h = hardware.parse(role, text)
        configs[role] = (vmid, text, cfg, h)
    root.mkdir(parents=True, mode=0o700)
    manifest = {}
    locked = []
    reserve = 4*1024**3
    try:
        for role, (vmid, text, cfg, h) in configs.items():
            # The PVE backup lock prevents a normal qm start during conversion.
            subprocess.run(['qm', 'set', vmid, '--lock', 'backup'], check=True)
            locked.append(vmid)
            if run('qm', 'status', vmid) != 'status: stopped':
                raise ValueError(f'VM {vmid} started during preflight')
            current = run('qm', 'config', vmid, '--current', '1')
            if '\n'.join(x for x in current.splitlines() if not x.startswith('lock: ')) != text:
                raise ValueError(f'VM {vmid}: config changed during preflight')
            (root/(role.replace('_','-')+'-config.txt')).write_text(text+'\n')
        for role, (vmid, text, cfg, h) in configs.items():
            if shutil.disk_usage(root).free < reserve:
                raise ValueError('Less than 4 GiB free; completed exports are retained')
            source = run('pvesm', 'path', cfg[h['disk_slot']].split(',')[0])
            jobs = [(role, source)]
            if h.get('efi_type'):
                jobs.append((role+'_efi', run('pvesm','path',cfg['efidisk0'].split(',')[0])))
            elif role == 'br_fw' and h['bios'] == 'ovmf':
                # Original PVE guest has no persistent EFI: save an equivalent
                # default 2m variables template, not an unrelated unused volume.
                template = Path('/usr/share/pve-edk2-firmware/OVMF_VARS.fd')
                if not template.is_file():
                    raise ValueError('Default OVMF variables template missing')
                h['efi_type'] = '2m'
                h['efi_origin'] = 'default-template: source VM had no persistent EFI disk'
                jobs.append((role+'_efi', str(template)))
            for key, source in jobs:
                filename = key.replace('_','-')+'.qcow2'
                dest = root / filename
                partial = root / (filename+'.partial')
                print('Exporting '+role+' ('+vmid+'): '+filename, flush=True)
                proc = subprocess.Popen(['qemu-img','convert','-p','-c','-O','qcow2',source,str(partial)])
                try:
                    while proc.poll() is None:
                        if shutil.disk_usage(root).free < reserve:
                            proc.terminate(); proc.wait()
                            raise ValueError('Export stopped to preserve 4 GiB free on host filesystem')
                        time.sleep(2)
                    if proc.returncode:
                        raise ValueError('qemu-img conversion failed: '+filename)
                finally:
                    if proc.poll() is None:
                        proc.terminate(); proc.wait()
                subprocess.run(['qemu-img','check',str(partial)],check=True)
                subprocess.run(['qemu-img','compare',source,str(partial)],check=True)
                os.replace(partial,dest)
                item = dict(id=None,file=filename,sha256=digest(dest),bytes=dest.stat().st_size,
                            virtual_size=json.loads(run('qemu-img','info','--output=json',str(dest)))['virtual-size'])
                if key == role:
                    item['hardware'] = h
                manifest[key] = item
                save(root/'module2-images.pending.json',manifest)
        save(root/'module2-images.pending.json',manifest)
        (root/'READY.txt').write_text('Module 2 baseline: Module 1 guest settings saved.\nUpload qcow2 files and fill Drive IDs in module2-images.pending.json.\nSource configs saved for recovery; no snapshot history or RAM state exported.\nDo not start source ISP and its copy simultaneously on the same uplink.\n')
        print('Export complete: '+str(root),flush=True)
    finally:
        for vmid in reversed(locked):
            if subprocess.run(['qm','unlock',vmid]).returncode:
                print('WARNING: could not unlock VM '+vmid,file=sys.stderr)

if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        sys.exit('ERROR: '+str(error))
