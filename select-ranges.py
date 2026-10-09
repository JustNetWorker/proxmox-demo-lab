#!/usr/bin/env python3
"""Choose consecutive unused VMIDs and Linux bridge numbers (read-only)."""
import json
import re
import subprocess
import sys
from pathlib import Path


def first_free(used, start, count, maximum):
    candidate = start
    for number in sorted(n for n in used if n >= start):
        if number >= candidate + count:
            break
        if number >= candidate:
            candidate = number + 1
    if candidate + count - 1 > maximum:
        raise ValueError('No free consecutive range available')
    return candidate


def bridge_ids(net=Path('/sys/class/net'), interfaces=Path('/etc/network/interfaces')):
    names = [p.name for p in net.iterdir()]
    files = [interfaces]
    snippets = interfaces.parent / 'interfaces.d'
    if snippets.is_dir():
        files.extend(p for p in snippets.rglob('*') if p.is_file())
    for path in files:
        if path.exists():
            names.append(path.read_text())
    return {int(n) for text in names for n in re.findall(r'(?<![\w.-])vmbr([0-9]+)(?![\w.-])', text)}


def main():
    base, bridge = sys.argv[1:]
    if base == 'auto':
        resources = json.loads(subprocess.check_output([
            'pvesh', 'get', '/cluster/resources', '--type', 'vm', '--output-format', 'json']))
        base = first_free({int(x['vmid']) for x in resources}, 200, 8, 999999999)
    if bridge == 'auto':
        bridge = first_free(bridge_ids(), 201, 9, 9999)
    print(base, bridge)


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        sys.exit(f'Cannot select free ranges: {exc}')
