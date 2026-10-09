#!/usr/bin/env python3
"""Build a private BIOS autoinstall ISO; never embeds credentials in GitHub."""
import argparse, io, json, os, re, subprocess, tarfile
from pathlib import Path
import pycdlib


def build(source, destination, role, password, work):
    iso = pycdlib.PyCdlib()
    iso.open(str(source))
    try:
        def extract(path):
            buf = io.BytesIO()
            iso.get_file_from_iso_fp(buf, rr_path=path)
            return buf.getvalue()
        cfg = extract('/syslinux/isolinux.cfg').decode()
        packages = extract('/Metadata/pkg-groups.tar')
    finally:
        iso.close()
    with tarfile.open(fileobj=io.BytesIO(packages)) as tf:
        base = tf.extractfile('lists/.base').read().decode()
    if 'alterator-postinstall' not in base:
        raise RuntimeError('ISO lacks alterator-postinstall; refusing unverified image')
    blocks = re.split(r'(?im)(?=^label\s+)', cfg)
    found = False
    for index, block in enumerate(blocks):
        if re.match(r'(?im)^label\s+linux\s*$', block):
            if not re.search(r'(?im)^\s*append .*stagename=', block):
                raise RuntimeError('Unrecognized ALT boot entry')
            blocks[index] = re.sub(r'(?im)^(\s*append [^\n]*)$',
                r'\1 ai curl=http://127.0.0.1:9/Metadata/', block)
            found = True
    if not found:
        raise RuntimeError('No ALT installation boot entry')
    cfg = ''.join(blocks)
    cfg = re.sub(r'(?im)^timeout\s+\d+\s*$', 'timeout 10', cfg)
    cfg = re.sub(r'(?im)^totaltimeout\s+\d+\s*$', 'totaltimeout 20', cfg)
    cfg = re.sub(r'(?im)^default\s+\S+\s*$', 'default linux', cfg)
    # Force a plain text Syslinux menu, including on Workstation gfxboot media.
    cfg = re.sub(r'(?im)^ui\s+.*$', '', cfg)
    metadata = work / 'Metadata'
    metadata.mkdir(parents=True, exist_ok=True)
    (work / 'isolinux.cfg').write_text(cfg)
    (metadata / 'pkg-groups.tar').write_bytes(packages)
    # Scheme strings use the same escapes for quotes/backslashes as JSON strings.
    quoted = json.dumps(password, ensure_ascii=False)
    hostname = role.lower()
    commands = [
        '("/sysconfig-base/language" action "write" lang ("ru_RU"))',
        '("/sysconfig-base/kbd" language ("ru_RU") action "write" layout "alt_sh_toggle")',
        '("/datetime-installer" action "write" commit #t name "RU" zone "Europe/Moscow" utc #t)',
        '("/evms/control" action "write" control open installer #t)',
        '("/evms/control" action "write" control update)',
        '("/evms/profiles/demo" action apply commit #f clearall #t exclude ())',
        '("/evms/control" action "write" control commit)',
        '("/evms/control" action "write" control close)',
        '("pkg-install-init" action "write")',
        '("/pkg-install" action "write" lists "" auto #t)',
        '("/preinstall" action "write")',
        '("/grub" action "write" device "/dev/sda" passwd #f passwd_1 "*" passwd_2 "*")',
        f'("/root/change_password" passwd_1 {quoted} passwd_2 {quoted})',
        f'("/users/create_account" new_name "admin" real_name "Lab administrator" allow_su #t auto #f passwd_1 {quoted} passwd_2 {quoted} autologin #f)',
        # This executes in the installed OS on its first boot; installs remain offline.
        '("/postinstall/firsttime" run "touch /root/demo-installed; echo DEMO_INSTALL_OK >/dev/ttyS0; sleep 2; systemctl poweroff")',
    ]
    (metadata / 'autoinstall.scm').write_text('\n'.join(commands) + '\n')
    (metadata / 'vm-profile.scm').write_text('''((demo
 (title . "Demo lab") (action . trivial)
 (actiondata
  ("swap" (size 2097152 . 2097152) (fsim . "SWAPFS") (methods plain))
  ("/" (size 16777216 . #t) (fsim . "Ext4") (methods plain)))))
''')
    # Postinstall runs in the installer; paths use destdir, not the host root.
    post = f'''#!/bin/sh
set -eu
printf '%s\\n' '{hostname}' > "$destdir/etc/hostname"
printf '%s\\n' '127.0.0.1 localhost {hostname}' > "$destdir/etc/hosts"
'''
    archive = metadata / 'install-scripts.tar'
    with tarfile.open(archive, 'w') as tf:
        data = post.encode()
        entry = tarfile.TarInfo('postinstall.d/99-demo-hostname')
        entry.mode = 0o755
        entry.size = len(data)
        tf.addfile(entry, io.BytesIO(data))
    subprocess.run(['xorriso', '-indev', str(source), '-outdev', str(destination),
        '-boot_image', 'any', 'replay', '-map', str(metadata), '/Metadata',
        '-map', str(work / 'isolinux.cfg'), '/syslinux/isolinux.cfg'], check=True)
    os.chmod(destination, 0o600)
    check = pycdlib.PyCdlib(); check.open(str(destination))
    try:
        for path in ['/Metadata/autoinstall.scm', '/Metadata/vm-profile.scm',
                     '/Metadata/pkg-groups.tar', '/Metadata/install-scripts.tar']:
            check.get_record(rr_path=path)
    finally:
        check.close()


if __name__ == '__main__':
    p = argparse.ArgumentParser()
    p.add_argument('source', type=Path); p.add_argument('destination', type=Path)
    p.add_argument('role', choices=['ISP', 'HQ-SW', 'HQ-SRV', 'BR-SRV', 'HQ-CLI'])
    p.add_argument('work', type=Path); p.add_argument('password_file', type=Path)
    a = p.parse_args()
    build(a.source, a.destination, a.role, a.password_file.read_text(), a.work)
