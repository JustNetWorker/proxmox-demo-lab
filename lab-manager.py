#!/usr/bin/env python3
"""Inventory, inspect and maintain one deployment, never arbitrary VMID ranges."""
import argparse
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

import importlib.util
HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('images', HERE / 'image-manifest.py')
images = importlib.util.module_from_spec(spec)
spec.loader.exec_module(images)
HOME = Path('/root')
INTERFACES = Path('/etc/network/interfaces')
VM_CONFIG_DIRS = (Path('/etc/pve/qemu-server'), Path('/etc/pve/lxc'))
LOCK = Path('/run/lock/proxmox-demo-deploy.lock')
NET = Path('/sys/class/net')
ROLES = ['ISP', 'HQ-RTR', 'HQ-SW', 'HQ-SRV', 'HQ-CLI', 'BR-RTR', 'BR-FW', 'BR-SRV']
DISK = re.compile(r'(?:scsi|sata|ide|virtio|unused)[0-9]+|efidisk0|tpmstate0')


def run(*args):
    return subprocess.check_output([str(x) for x in args], text=True).strip()


def config(vmid):
    return dict(line.split(': ', 1) for line in run('qm', 'config', vmid, '--current', '1').splitlines() if ': ' in line)


def volumes(cfg):
    return {k: v.split(',')[0] for k, v in cfg.items()
            if (DISK.fullmatch(k) or k == 'rootfs' or re.fullmatch(r'mp[0-9]+', k)) and 'media=cdrom' not in v and v.split(',')[0] != 'none'}


def machine_uuid(cfg):
    match = re.search(r'(?:^|,)uuid=([0-9a-fA-F-]{36})(?:,|$)', cfg.get('smbios1', ''))
    if not match:
        raise ValueError('VM SMBIOS UUID is missing')
    return match.group(1).lower()


def save(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n')
    tmp.chmod(0o600)
    os.replace(tmp, path)


def journal_path(value):
    p = Path(value)
    if p.parent != HOME or not re.fullmatch(r'demo-deploy\.[A-Za-z0-9]{8}', p.name) or p.is_symlink():
        raise ValueError('Choose an original /root/demo-deploy.XXXXXXXX journal')
    if not p.is_dir() or p.stat().st_uid != os.geteuid():
        raise ValueError('Journal directory unavailable or owned by another user')
    return p


def load(value):
    p = journal_path(value)
    file = p / 'lab.json'
    if file.is_symlink():
        raise ValueError('Symlink inventory is not supported')
    d = json.loads(file.read_text())
    if d.get('version') != 1 or d.get('node') != socket.gethostname().split('.')[0] or d.get('journal') != str(p):
        raise ValueError('Journal belongs to another node or has an unsupported format')
    ids = [v['id'] for v in d['vms']]
    if len(set(ids)) != len(ids) or len(ids) > 8 or any(not isinstance(i, int) or not 100 <= i <= 999999999 for i in ids):
        raise ValueError('Invalid VM inventory')
    for b in d['bridges']:
        if not re.fullmatch(r'vmbr[0-9]{1,4}', b['name']) or not b['block'].startswith('\n# proxmox-demo-lab '):
            raise ValueError('Invalid bridge inventory')
    return p, d


def marker(p):
    return 'proxmox-demo-lab: journal ' + str(p)


def verify_vm(p, vm, cfg):
    if vm.get('marker', marker(p)) not in cfg.get('description', ''):
        raise ValueError(f"VM {vm['id']}: deployment marker changed")
    if machine_uuid(cfg) != vm['uuid'] or cfg.get('name') != vm['name']:
        raise ValueError(f"VM {vm['id']}: identity/name changed; not deleting or snapshotting it")
    if volumes(cfg) != vm['volumes']:
        raise ValueError(f"VM {vm['id']}: disk set changed")
    if {k: v for k, v in cfg.items() if re.fullmatch(r'net[0-9]+', k)} != vm['networks']:
        raise ValueError(f"VM {vm['id']}: network adapters changed")
    for v in volumes(cfg).values():
        if not re.search(r'(?:^|[/:])(?:[0-9]+/)?vm-' + str(vm['id']) + r'-', v):
            raise ValueError(f"VM {vm['id']}: volume does not have the expected VM owner: {v}")


def initialize(args):
    p = journal_path(args.journal)
    if (p / 'lab.json').exists():
        raise ValueError('Inventory already exists')
    save(p / 'lab.json', dict(version=1, node=socket.gethostname().split('.')[0], journal=str(p),
         profile=args.profile, cache=args.image_dir, complete=False, vms=[], bridges=[], snapshots=[], deleted=False))


def record_vm(args):
    p, d = load(args.journal)
    cfg = config(args.vmid)
    if marker(p) not in cfg.get('description', ''):
        raise ValueError('Cannot record a VM without its deployment marker')
    vm = dict(id=args.vmid, name=cfg['name'], uuid=machine_uuid(cfg), volumes=volumes(cfg),
              networks={k: v for k, v in cfg.items() if re.fullmatch(r'net[0-9]+', k)},
              memory=int(cfg['memory']), cores=int(cfg['cores']))
    d['vms'] = [v for v in d['vms'] if v['id'] != args.vmid] + [vm]
    save(p / 'lab.json', d)


def record_bridge(args):
    p, d = load(args.journal)
    if any(b['name'] == args.bridge for b in d['bridges']):
        raise ValueError('Bridge already recorded')
    block = Path(args.block_file).read_text()
    if not re.fullmatch(r'vmbr[0-9]{1,4}', args.bridge) or f'iface {args.bridge} inet manual\n' not in block or block not in INTERFACES.read_text():
        raise ValueError('Bridge block was not installed')
    d['bridges'].append(dict(name=args.bridge, block=block))
    save(p / 'lab.json', d)


def finish(args):
    p, d = load(args.journal)
    if len(d['vms']) != 8 or {v['name'] for v in d['vms']} != set(ROLES) or len(d['bridges']) != 9:
        raise ValueError('Deployment inventory is incomplete')
    d['complete'] = True
    save(p / 'lab.json', d)


def journals():
    for p in sorted(HOME.glob('demo-deploy.*'), key=lambda p: p.stat().st_mtime, reverse=True):
        if (p / 'lab.json').exists():
            try:
                _, d = load(str(p))
                state = 'удалён' if d['deleted'] else ('готов' if d['complete'] else 'частично создан')
                print(f"{p} | {d['profile']} | {state} | VMID: " + ', '.join(str(v['id']) for v in d['vms']))
            except (ValueError, OSError, KeyError) as exc:
                print(f'{p}: {exc}')
        elif (p / 'created.tsv').exists():
            print(f'{p} | старый журнал; при выборе будет предложена регистрация')


def adopt_legacy(args):
    p = journal_path(args.journal)
    if (p / 'lab.json').exists(): return
    table = p / 'created.tsv'
    backup = p / 'interfaces.before'
    if table.is_symlink() or backup.is_symlink() or not table.is_file() or not backup.is_file():
        raise ValueError('Original created.tsv and interfaces.before are required')
    rows = [line.split('\t') for line in table.read_text().splitlines() if line]
    ids = [int(row[0]) for row in rows]
    if len(ids) != 8 or len(set(ids)) != 8 or ids != list(range(ids[0], ids[0]+8)):
        raise ValueError('Only a complete eight-VM legacy deployment can be registered')
    resources = existing_resources()
    vms = []
    legacy_marker = 'proxmox-demo-lab: imported OS and hardware; journal ' + str(p)
    for i in ids:
        if resources.get(i, {}).get('type') != 'qemu' or resources[i].get('node') != socket.gethostname().split('.')[0]:
            raise ValueError('Legacy VM is missing or migrated: ' + str(i))
        cfg = config(i)
        vm = dict(id=i, name=cfg['name'], uuid=machine_uuid(cfg), marker=legacy_marker,
                  volumes=volumes(cfg), networks={k:v for k,v in cfg.items() if re.fullmatch(r'net[0-9]+',k)},
                  memory=int(cfg['memory']), cores=int(cfg['cores']))
        verify_vm(p, vm, cfg)
        if not any(k in vm['volumes'] for k in ('scsi0','sata0')):
            raise ValueError('Legacy VM import is incomplete: ' + str(i))
        vms.append(vm)
    text = INTERFACES.read_text()
    pattern = (r'\n# proxmox-demo-lab ' + str(ids[0]) + r'\nauto (vmbr[0-9]+)\niface \1 inet manual\n'
               r'    bridge-ports none\n    bridge-stp off\n    bridge-fd 0\n')
    bridges = [dict(name=match[1], block=match[0]) for match in re.finditer(pattern, text)]
    if len(bridges) != 9 or any(b['name'] in backup.read_text() for b in bridges):
        raise ValueError('Cannot identify nine original isolated bridges; registration refused')
    networks = ','.join(v for vm in vms for v in vm['networks'].values())
    if any('bridge='+b['name']+',' not in networks+',' for b in bridges):
        raise ValueError('Legacy bridge topology changed')
    print('Будет зарегистрирован старый стенд без изменения ВМ:')
    for vm in vms: print(f"  {vm['id']} {vm['name']} | " + ', '.join(vm['volumes'].values()))
    print('Мосты: ' + ', '.join(b['name'] for b in bridges))
    if not confirm('REGISTER ' + p.name): raise ValueError('Registration cancelled')
    for vm in vms: verify_vm(p, vm, config(vm['id']))
    save(p/'lab.json', dict(version=1, node=socket.gethostname().split('.')[0], journal=str(p), profile='legacy',
         cache='', complete=True, vms=vms, bridges=bridges, snapshots=[], deleted=False))
    print('Старый стенд зарегистрирован. ВМ и сеть не изменены.')


def inspect_lab(args):
    p, d = load(args.journal)
    print(f"Стенд: {p.name}; профиль: {d['profile']}; завершён: {d['complete']}; удалён: {d['deleted']}")
    problems = 0
    ram = cores = 0
    for vm in d['vms']:
        try:
            cfg = config(vm['id'])
            verify_vm(p, vm, cfg)
            state = run('qm', 'status', vm['id'])
            notes = []
            if 'lock' in cfg: notes.append('блокировка: ' + cfg['lock'])
            if cfg.get('memory') != str(vm['memory']) or cfg.get('cores') != str(vm['cores']): notes.append('ресурсы изменены')
            if d['complete'] and not any(k in volumes(cfg) for k in ('scsi0', 'sata0')): notes.append('нет системного диска')
            for k, v in vm['networks'].items():
                b = re.search(r'(?:^|,)bridge=([^,]+)', v)
                if not b or not (NET / b[1] / 'bridge').is_dir(): notes.append(k + ': мост не активен')
            for v in volumes(cfg).values():
                volume_path = run('pvesm', 'path', v)
                if not Path(volume_path).exists(): notes.append('диск недоступен: ' + v)
            ram += int(cfg['memory']); cores += int(cfg['cores'])
            problems += bool(notes)
            print(f"{vm['id']} {cfg['name']}: {state}; RAM {cfg['memory']} МиБ; CPU {cfg['cores']}; дисков {len(volumes(cfg))}; карт {len(vm['networks'])}" + (' | ' + '; '.join(notes) if notes else ' | OK'))
        except (ValueError, OSError, subprocess.CalledProcessError) as exc:
            problems += 1
            print(f"{vm['id']} {vm['name']}: ПРОБЛЕМА: {exc}")
    print(f'Всего: {ram} МиБ ОЗУ и {cores} vCPU; ВМ с замечаниями: {problems}.')
    print('Это проверка оборудования. Адресация, интернет и службы внутри ОС здесь не проверяются.')
    return 1 if problems else 0


def existing_resources():
    return {int(x['vmid']): x for x in json.loads(run('pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json'))}


def safe_stopped(p, vm):
    cfg = config(vm['id'])
    verify_vm(p, vm, cfg)
    if 'lock' in cfg or run('qm', 'status', vm['id']) != 'status: stopped':
        raise ValueError(f"VM {vm['id']}: first shut it down normally and wait for active tasks; lock bypass is not used")
    return cfg


def confirm(text):
    try:
        return input('Для подтверждения введи ' + text + ': ').strip() == text
    except EOFError:
        return False


def bridge_removal_plan(d, removing):
    if INTERFACES.is_symlink() or not INTERFACES.is_file() or Path(str(INTERFACES) + '.new').exists():
        raise ValueError('Apply/discard pending network changes before deleting bridges')
    text = INTERFACES.read_text()
    removable = []
    for b in d['bridges']:
        if b.get('deleted'): continue
        if text.count(b['block']) != 1:
            raise ValueError(b['name'] + ': configuration block changed; bridge deletion refused')
        remainder = text.replace(b['block'], '', 1)
        token = re.compile(r'(?<![\w.-])' + re.escape(b['name']) + r'(?![\w.-])')
        if token.search(remainder):
            raise ValueError(b['name'] + ': referenced elsewhere in interfaces')
        snippets = INTERFACES.parent / 'interfaces.d'
        for path in snippets.rglob('*') if snippets.is_dir() else []:
            if path.is_file() and token.search(path.read_text()):
                raise ValueError(b['name'] + ': referenced in a network snippet')
        for directory in VM_CONFIG_DIRS:
            for path in directory.glob('*.conf'):
                if directory.name == 'qemu-server' and path.stem.isdigit() and int(path.stem) in removing: continue
                if token.search(path.read_text()):
                    raise ValueError(b['name'] + ': used by another VM or container: ' + path.name)
        brif = NET / b['name'] / 'brif'
        if brif.is_dir() and any(brif.iterdir()):
            raise ValueError(b['name'] + ': bridge still has live ports; deletion refused')
        if (NET / b['name']).exists():
            addresses = json.loads(run('ip', '-j', 'addr', 'show', 'dev', b['name']))
            if any(a.get('family') == 'inet' or a.get('scope') == 'global'
                   for entry in addresses for a in entry.get('addr_info', [])):
                raise ValueError(b['name'] + ': bridge has a host IP address; deletion refused')
        removable.append(b)
    return removable


def check_shared_disks(d, removing):
    owned_volumes = {v for vm in d['vms'] if vm['id'] in removing for v in vm['volumes'].values()}
    for directory in VM_CONFIG_DIRS:
        for path in directory.glob('*.conf'):
            if directory.name == 'qemu-server' and path.stem.isdigit() and int(path.stem) in removing: continue
            if any(v in path.read_text() for v in owned_volumes):
                raise ValueError('An owned disk is referenced by another VM/container: ' + path.name)


def delete_lab(args):
    p, d = load(args.journal)
    if d['deleted']:
        print('Стенд уже удалён. Журнал сохранён.'); return
    resources = existing_resources()
    removing = set()
    for vm in d['vms']:
        if vm['id'] not in resources:
            print(f"{vm['id']} уже отсутствует"); continue
        r = resources[vm['id']]
        if r.get('type') != 'qemu' or r.get('node') != d['node']:
            raise ValueError(f"VMID {vm['id']} now belongs to another VM/container or node")
        safe_stopped(p, vm)
        removing.add(vm['id'])
    check_shared_disks(d, removing)
    bridges = bridge_removal_plan(d, removing)
    print('Будут удалены ВМ и их подключённые диски:')
    for vm in d['vms']:
        if vm['id'] in removing: print(f"  {vm['id']} {vm['name']}: " + ', '.join(vm['volumes'].values()))
    print('Мосты: ' + ', '.join(b['name'] for b in bridges))
    print('Кэш qcow2 и журнал сохраняются. Запущенные ВМ автоматически не выключаются.')
    if not confirm('DELETE ' + p.name): print('Удаление отменено.'); return
    # Revalidate after user input; never override PVE locks or destroy unrelated disks.
    for vm in d['vms']:
        if vm['id'] in removing: safe_stopped(p, vm)
    bridge_removal_plan(d, removing)
    check_shared_disks(d, removing)
    for vm in d['vms']:
        if vm['id'] in removing:
            safe_stopped(p, vm)
            check_shared_disks(d, removing)
            subprocess.run(['qm', 'destroy', str(vm['id'])], check=True)
            vm['deleted'] = True
            save(p / 'lab.json', d)
    # No bridge is removed unless all planned VMs were successfully removed.
    if any(i in existing_resources() for i in removing): raise ValueError('Some VMIDs still exist; bridges retained')
    for b in bridge_removal_plan(d, set()):
        backup = p / f"interfaces.before-delete-{time.time_ns()}"
        backup.write_bytes(INTERFACES.read_bytes())
        if (NET / b['name']).exists():
            subprocess.run(['ifdown', b['name']], check=True)
        text = INTERFACES.read_text()
        if text.count(b['block']) != 1: raise ValueError('Bridge config changed during deletion')
        INTERFACES.write_text(text.replace(b['block'], '', 1))
        b['deleted'] = True
        save(p / 'lab.json', d)
    d['deleted'] = True
    save(p / 'lab.json', d)
    print('Стенд удалён. Журнал и резервные копии настроек сети сохранены: ' + str(p))


def snapshots(args):
    p, d = load(args.journal)
    if not d['complete'] or d['deleted'] or len(d['vms']) != 8:
        raise ValueError('Initial snapshots require a complete, undeleted deployment')
    name = 'lab-initial'
    candidates = []
    for vm in d['vms']:
        safe_stopped(p, vm)
        listing = json.loads(run('pvesh', 'get', f"/nodes/{d['node']}/qemu/{vm['id']}/snapshot", '--output-format', 'json'))
        if any(x['name'] == name for x in listing):
            print(f"{vm['id']}: снимок {name} уже существует; не перезаписываю"); continue
        feature = json.loads(run('pvesh', 'get', f"/nodes/{d['node']}/qemu/{vm['id']}/feature", '--feature', 'snapshot', '--output-format', 'json'))
        if not feature.get('hasFeature'):
            raise ValueError(f"VM {vm['id']}: storage/disks do not support snapshots; no new snapshots created")
        candidates.append(vm)
    if not candidates: print('Все начальные снимки уже существуют.'); return
    print('Снимки дисков без памяти будут созданы для: ' + ', '.join(str(v['id']) for v in candidates))
    for vm in candidates:
        safe_stopped(p, vm)
        subprocess.run(['qm', 'snapshot', str(vm['id']), name, '--vmstate', '0', '--description', 'Initial state: ' + p.name], check=True)
        d['snapshots'].append(dict(vmid=vm['id'], name=name))
        save(p / 'lab.json', d)
        print(f"{vm['id']}: {name} создан")
    print('Снимки готовы. Откат доступен в Proxmox → ВМ → Snapshots.')


def clean_cache(args):
    root = Path(args.image_dir)
    if not root.is_absolute() or root.is_symlink() or root.resolve() == Path('/') or not root.is_dir():
        raise ValueError('Choose an existing absolute cache directory; symlinks are not supported')
    manifests = [(images.load_manifest(HERE / 'images.json'), root)]
    for profile in ('module1', 'module2'):
        module = HERE / (profile + '-images.json')
        if module.exists(): manifests.append((images.load_manifest(module), root / profile))
    files = []
    for manifest, directory in manifests:
        if directory.is_symlink(): raise ValueError('Symlink cache directory is not supported')
        for item in manifest.values():
            for suffix in ('', '.demo.json', '.demo-download'):
                p = directory / (item['file'] + suffix)
                if p.is_symlink(): raise ValueError('Symlink cache entry: ' + str(p))
                if p.exists():
                    if not p.is_file(): raise ValueError('Unexpected cache directory: ' + str(p))
                    files.append(p)
    files = list(dict.fromkeys(files))
    if not files: print('Распознанных файлов кэша нет.'); return
    for directory in VM_CONFIG_DIRS:
        for p in directory.glob('*.conf'):
            cfg = dict(line.split(': ', 1) for line in p.read_text().splitlines() if ': ' in line)
            for v in volumes(cfg).values():
                vpath = Path(v if v.startswith('/') else run('pvesm', 'path', v)).resolve()
                if vpath.is_relative_to(root.resolve()): raise ValueError('A VM/CT uses a disk inside this cache: ' + str(p))
    print('Будут удалены только распознанные файлы кэша:')
    for p in files: print('  ' + str(p))
    print('Объём: %.2f ГиБ. Неизвестные файлы сохраняются.' % (sum(p.stat().st_size for p in files) / 1024**3))
    if not confirm('CLEAR'): print('Очистка отменена.'); return
    for p in files:
        if p.is_symlink() or not p.is_file(): raise ValueError('Cache changed during confirmation')
        p.unlink()
    print('Кэш очищен. Импортированные диски ВМ сохранены; при новом развёртывании образы будут скачаны снова.')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['list', 'adopt', 'init', 'record-vm', 'record-bridge', 'finish', 'check', 'delete', 'snapshots', 'clean-cache'])
    parser.add_argument('--journal')
    parser.add_argument('--vmid', type=int)
    parser.add_argument('--bridge')
    parser.add_argument('--block-file')
    parser.add_argument('--image-dir', default='/var/lib/vz/demo-images')
    parser.add_argument('--profile', default='base')
    args = parser.parse_args()
    if args.action == 'list': journals(); return
    if args.action in ('init', 'record-vm', 'record-bridge', 'finish'):
        {'init': initialize, 'record-vm': record_vm, 'record-bridge': record_bridge, 'finish': finish}[args.action](args); return
    # Same lock as deployment: cleanup/snapshot cannot overlap import.
    with LOCK.open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return {'adopt': adopt_legacy, 'check': inspect_lab, 'delete': delete_lab, 'snapshots': snapshots, 'clean-cache': clean_cache}[args.action](args)


if __name__ == '__main__':
    try:
        if os.geteuid() != 0: raise ValueError('Run in Proxmox Shell as root')
        sys.exit(main() or 0)
    except (ValueError, OSError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        sys.exit('ERROR: ' + str(exc))
