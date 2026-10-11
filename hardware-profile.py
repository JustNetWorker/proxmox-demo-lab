#!/usr/bin/env python3
"""Read only supported hardware fields; never execute an exported PVE config."""
import json
import re
import sys
import uuid
from pathlib import Path

LINKS = {
    'isp': ['uplink', 1, 2], 'hq_rtr': [8, 1, 3], 'hq_sw': [3, 4, 5],
    'hq_srv': [4], 'hq_cli': [5], 'br_rtr': [9, 2, 6],
    'br_fw': [6, 7], 'br_srv': [7],
}
MODELS = {'virtio', 'e1000', 'e1000e', 'rtl8139', 'vmxnet3'}
SCSI = {'virtio-scsi-single', 'virtio-scsi-pci', 'lsi', 'lsi53c810', 'megasas', 'pvscsi'}

def validate(role, h):
    if role not in LINKS or not isinstance(h, dict):
        raise ValueError('Unknown role or missing hardware metadata')
    for key in ('bios', 'machine', 'disk_slot', 'scsihw', 'uuid'):
        if not isinstance(h.get(key), str):
            raise ValueError(role + ': missing or invalid ' + key)
    if h.get('bios') not in {'seabios', 'ovmf'}:
        raise ValueError(role + ': unsupported BIOS')
    if not re.fullmatch(r'(?:pc|q35|pc-(?:i440fx|q35)-[0-9.]+)', h.get('machine', '')):
        raise ValueError(role + ': unsupported machine type')
    if not re.fullmatch(r'(?:ide[0-3]|scsi(?:[0-9]|[12][0-9]|30)|sata[0-5]|virtio(?:[0-9]|1[0-5]))', h.get('disk_slot', '')):
        raise ValueError(role + ': invalid disk slot')
    if h.get('scsihw') not in SCSI:
        raise ValueError(role + ': unsupported SCSI controller')
    if not re.fullmatch(r'[0-9a-fA-F-]{36}', h['uuid']):
        raise ValueError(role + ': invalid UUID')
    uuid.UUID(h['uuid'])
    nics = h.get('nics', [])
    if not isinstance(nics, list) or any(not isinstance(n, dict) for n in nics):
        raise ValueError(role + ': invalid NIC metadata')
    if len(nics) != len(LINKS[role]) or {str(n.get('link')) for n in nics} != {str(x) for x in LINKS[role]}:
        raise ValueError(role + ': network topology does not match the lab')
    for n in nics:
        if not isinstance(n.get('mac'), str) or not isinstance(n.get('model'), str):
            raise ValueError(role + ': invalid NIC metadata')
        if n.get('model') not in MODELS or not re.fullmatch(r'(?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}', n.get('mac', '')):
            raise ValueError(role + ': invalid NIC model or MAC')
    if h.get('efi_type') not in {None, '2m', '4m'}:
        raise ValueError(role + ': unsupported EFI type')
    if h.get('efi_type') and role != 'br_fw':
        raise ValueError(role + ': this profile only supports a separate EFI disk for BR-FW')
    if h.get('efi_type') and h['bios'] != 'ovmf':
        raise ValueError(role + ': EFI disk requires OVMF')
    if not isinstance(h.get('serial', False), bool):
        raise ValueError(role + ': invalid serial option')
    return h

def parse(role, text):
    cfg = dict(x.split(': ', 1) for x in text.splitlines() if ': ' in x)
    disks = [k for k,v in cfg.items() if re.fullmatch(r'(?:ide[0-3]|scsi(?:[0-9]|[12][0-9]|30)|sata[0-5]|virtio(?:[0-9]|1[0-5]))', k) and 'media=cdrom' not in v and not v.startswith('none')]
    if len(disks) != 1 or 'tpmstate0' in cfg:
        raise ValueError(role + ': export supports exactly one system disk and no TPM')
    nics = []
    for i in range(len(LINKS[role])):
        value = cfg.get('net' + str(i), '')
        match = re.fullmatch(r'([a-z0-9]+)=((?:[0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2})', value.split(',')[0])
        bridge = re.search(r'(?:^|,)bridge=(vmbr[0-9]+)(?:,|$)', value)
        if not match or not bridge or re.search(r'(?:^|,)(?:tag|trunks|link_down|rate)=', value):
            raise ValueError(role + ': unsupported source NIC settings')
        link = 'uplink' if role == 'isp' and i == 0 else int(bridge[1][4:])
        nics.append(dict(model=match[1], mac=match[2].upper(), link=link))
    if len([k for k in cfg if re.fullmatch(r'net[0-9]+', k)]) != len(nics):
        raise ValueError(role + ': unexpected extra NIC')
    match = re.search(r'(?:^|,)uuid=([0-9A-Fa-f-]{36})(?:,|$)', cfg.get('smbios1', ''))
    if not match:
        raise ValueError(role + ': SMBIOS UUID missing')
    h = dict(bios=cfg.get('bios', 'seabios'), machine=cfg.get('machine', 'pc'),
             disk_slot=disks[0], scsihw=cfg.get('scsihw', 'lsi'), uuid=match[1],
             nics=nics, serial=cfg.get('serial0') == 'socket')
    if 'efidisk0' in cfg:
        h['efi_type'] = '4m' if 'efitype=4m' in cfg['efidisk0'] else '2m'
    return validate(role, h)

def arguments(role, h, volume, base, uplink, efivolume=None):
    validate(role, h)
    args = ['--bios', h['bios'], '--machine', h['machine'], '--scsihw', h['scsihw'],
            '--smbios1', 'uuid=' + h['uuid'], '--' + h['disk_slot'], volume,
            '--boot', 'order=' + h['disk_slot']]
    if h.get('serial'):
        args += ['--serial0', 'socket']
    for i,n in enumerate(h['nics']):
        bridge = uplink if n['link'] == 'uplink' else 'vmbr' + str(base + int(n['link']) - 1)
        args += ['--net'+str(i), f"{n['model']}={n['mac']},bridge={bridge},firewall=0"]
    if h.get('efi_type'):
        if not efivolume:
            raise ValueError(role + ': exported EFI disk missing')
        args += ['--efidisk0', efivolume + ',efitype=' + h['efi_type'] + ',pre-enrolled-keys=0']
    return args

if __name__ == '__main__':
    try:
        manifest, role, volume, base, uplink = sys.argv[1:6]
        h = json.loads(Path(manifest).read_text())[role]['hardware']
        print('\n'.join(arguments(role, h, volume, int(base), uplink, sys.argv[6] if len(sys.argv)>6 else None)))
    except (ValueError, KeyError, IndexError, TypeError) as error:
        sys.exit('ERROR: ' + str(error))
