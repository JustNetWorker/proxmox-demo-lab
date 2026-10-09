#!/usr/bin/env python3
"""Download and inspect user-supplied disk images; exported configs are data only."""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

KINDS = {'jeos', 'server', 'cli', 'router', 'ideco', 'ideco_efi', 'ideco_config'}
ROLE_KINDS = {'isp', 'hq_rtr', 'hq_sw', 'hq_srv', 'hq_cli', 'br_rtr', 'br_fw', 'br_srv', 'br_fw_efi', 'br_fw_config'}

def load_manifest(path):
    data = json.loads(Path(path).read_text())
    if set(data) not in (KINDS, ROLE_KINDS):
        raise ValueError('Manifest must contain either the base image keys or all ten module image keys')
    for kind, item in data.items():
        if not isinstance(item, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{10,}', item.get('id') or ''):
            raise ValueError(f'{kind}: Google Drive upload/link is not ready in images.json')
        filename = item.get('file', '')
        suffix = '.txt' if kind.endswith('_config') else '.qcow2'
        if not re.fullmatch(r'[A-Za-z0-9_.-]+', filename) or not filename.endswith(suffix):
            raise ValueError(f'{kind}: invalid filename')
        if item.get('sha256') and not re.fullmatch(r'[0-9a-f]{64}', item['sha256']):
            raise ValueError(f'{kind}: invalid checksum')
    if len({x['file'] for x in data.values()}) != len(data):
        raise ValueError('Duplicate image filenames')
    return data

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(4 * 1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def identity(path):
    text = Path(path).read_text()
    if len(text) > 65536:
        raise ValueError('Exported Ideco config unexpectedly large')
    lines = dict(line.split(': ', 1) for line in text.splitlines() if ': ' in line)
    if lines.get('bios') != 'ovmf' or lines.get('machine') != 'q35':
        raise ValueError('Ideco base must use OVMF and q35')
    if 'efitype=4m' not in lines.get('efidisk0', ''):
        raise ValueError('Expected the exported 4m EFI disk')
    match = re.search(r'(?:^|,)uuid=([0-9a-fA-F-]+)(?:,|$)', lines.get('smbios1', ''))
    if not match:
        raise ValueError('Ideco SMBIOS UUID missing')
    ident = str(uuid.UUID(match.group(1)))
    match = re.match(r'virtio=([0-9a-fA-F]{2}(?::[0-9a-fA-F]{2}){5})(?:,|$)', lines.get('net0', ''))
    if not match:
        raise ValueError('Ideco net0 VirtIO MAC missing')
    return ident, match.group(1).upper()

def download(manifest, root, state):
    import gdown
    root.mkdir(parents=True, exist_ok=True)
    for kind, item in manifest.items():
        path = root / item['file']
        stamp = Path(str(path) + '.demo.json')
        if path.exists():
            if not stamp.exists():
                # Accept a manually copied file only when it matches the published digest.
                expected_hash = item.get('sha256')
                if not expected_hash or digest(path) != expected_hash:
                    raise ValueError(f'Unknown existing cache file: {path}; no matching published checksum')
                stamp.write_text(json.dumps({'id': item['id'], 'sha256': expected_hash}) + '\n')
                print('Verified manually supplied file: ' + path.name)
            saved = json.loads(stamp.read_text())
            if saved.get('id') != item['id'] or saved.get('sha256') != digest(path):
                raise ValueError(f'Cache identity or checksum mismatch: {path}')
        else:
            # Reserve space based on configured compressed size, when available.
            expected = item.get('bytes', 0)
            if expected and shutil.disk_usage(root).free < expected + 1024**3:
                raise ValueError(f'Not enough cache space for {path.name}')
            partial = Path(str(path) + '.demo-download')
            print('Downloading: ' + path.name, flush=True)
            try:
                result = gdown.download(id=item['id'], output=str(partial), quiet=False,
                                        resume=True, use_cookies=False)
            except Exception as error:
                url = 'https://drive.google.com/file/d/' + item['id'] + '/view'
                hint = (' A local copy can be placed at ' + str(path) +
                        '; its published SHA256 will be checked.') if item.get('sha256') else ''
                raise ValueError('Google Drive could not download ' + path.name +
                                 ' (' + type(error).__name__ + '). Check public access at ' + url +
                                 ' or retry later. Existing completed downloads are retained.' + hint) from None
            if result is None:
                raise ValueError('Google Drive download failed: ' + path.name)
            if not partial.exists() or partial.stat().st_size == 0:
                raise ValueError('Empty download: ' + path.name)
            if expected and partial.stat().st_size != expected:
                raise ValueError('Download size mismatch: ' + path.name)
            actual = digest(partial)
            if item.get('sha256') and actual != item['sha256']:
                raise ValueError('Published checksum mismatch: ' + path.name)
            os.replace(partial, path)
            stamp.write_text(json.dumps({'id': item['id'], 'sha256': actual}) + '\n')
        if item.get('sha256') and digest(path) != item['sha256']:
            raise ValueError('Published checksum mismatch: ' + path.name)
    print('Downloads complete. Cache hashes detect later changes; they are not vendor signatures.')

def allocated_bytes(path):
    # Inspect guest-address extents, not compressed qcow2 file size. Round ranges
    # to 1 MiB and merge them to conservatively cover thin-pool allocation units.
    extents = json.loads(subprocess.check_output([
        'qemu-img', 'map', '--output=json', str(path)]))
    unit = 1024**2
    ranges = []
    for entry in extents:
        if entry.get('data') and not entry.get('zero'):
            start = entry['start'] // unit * unit
            end = (entry['start'] + entry['length'] + unit - 1) // unit * unit
            ranges.append((start, end))
    total = 0
    previous_end = 0
    for start, end in sorted(ranges):
        total += max(0, end - max(start, previous_end))
        previous_end = max(previous_end, end)
    return total

def capacity(path, storage_type):
    verified = json.loads(Path(path).read_text())
    kinds = ['jeos', 'router', 'jeos', 'server', 'cli', 'router',
             'ideco', 'server', 'ideco_efi']
    if set(verified) == ROLE_KINDS:
        kinds = ['isp', 'hq_rtr', 'hq_sw', 'hq_srv', 'hq_cli', 'br_rtr', 'br_fw', 'br_srv', 'br_fw_efi']
    virtual = sum(max(verified[k]['virtual_size'], 10*1024**3)
                  if k in ('router', 'hq_rtr', 'br_rtr') else verified[k]['virtual_size'] for k in kinds)
    if storage_type == 'lvmthin':
        initial = sum(verified[k]['allocated_bytes'] for k in kinds)
        reserve = max(10*1024**3, (initial+3)//4)
        required = initial + reserve
    else:
        initial = virtual
        reserve = 1024**3
        required = virtual + reserve
    return {'virtual_bytes': virtual, 'initial_bytes': initial,
            'reserve_bytes': reserve, 'required_kb': (required+1023)//1024}

def inspect(manifest, root, state):
    verified = {}
    for kind, item in manifest.items():
        path = root / item['file']
        if kind.endswith('_config'):
            identity(path)
            size = 0
            allocated = 0
        else:
            data = json.loads(subprocess.check_output(['qemu-img', 'info', '--output=json', str(path)]))
            if data.get('format') != 'qcow2' or data.get('backing-filename') or data.get('encrypted') or data.get('data-file') or data.get('format-specific', {}).get('data', {}).get('data-file'):
                raise ValueError(f'{kind}: standalone, unencrypted qcow2 required')
            subprocess.run(['qemu-img', 'check', str(path)], check=True)
            size = data['virtual-size']
            if kind.endswith('_efi') and not 1024**2 <= size <= 16 * 1024**2:
                raise ValueError('Unexpected EFI virtual disk size')
            if not kind.endswith('_efi') and size < 1024**3:
                raise ValueError(f'{kind}: unexpectedly small system disk')
            allocated = allocated_bytes(path)
        verified[kind] = {'path': str(path.resolve()), 'virtual_size': size,
                          'allocated_bytes': allocated}
    (state / 'verified.json').write_text(json.dumps(verified, indent=2) + '\n')

if __name__ == '__main__':
    try:
        command = sys.argv[1]
        if command == 'capacity':
            print(json.dumps(capacity(sys.argv[2], sys.argv[3])))
        elif command == 'identity':
            print('\n'.join(identity(sys.argv[2])))
        else:
            manifest = load_manifest(sys.argv[2])
            if command == 'validate':
                if len(sys.argv) > 3:
                    expected = KINDS if sys.argv[3] == 'base' else ROLE_KINDS
                    if set(manifest) != expected: raise ValueError('Manifest keys do not match the chosen profile')
                print('Manifest ready')
            elif command in ('download', 'inspect'):
                globals()[command](manifest, Path(sys.argv[3]), Path(sys.argv[4]))
            else:
                raise ValueError('Unknown command')
    except (ValueError, KeyError, IndexError, OSError, subprocess.CalledProcessError) as e:
        sys.exit('ERROR: ' + str(e))
