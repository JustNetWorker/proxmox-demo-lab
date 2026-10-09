#!/usr/bin/env python3
"""Read the host IPv4 default route; never modify the host network."""
import json
import re
import subprocess
import sys


def select_bridge(routes):
    defaults = [r for r in routes if r.get('dst') == 'default'
                and 'linkdown' not in r.get('flags', [])]
    if not defaults:
        raise ValueError('No active IPv4 default route. Specify --uplink vmbrN.')
    metric = min(int(r.get('metric', 0)) for r in defaults)
    best = [r for r in defaults if int(r.get('metric', 0)) == metric]
    devices = set()
    for route in best:
        if route.get('type', 'unicast') != 'unicast':
            raise ValueError('Default route is not unicast. Specify --uplink vmbrN.')
        hops = route.get('nexthops') or [route]
        for hop in hops:
            if 'linkdown' not in hop.get('flags', []):
                devices.add(hop.get('dev', ''))
    if len(devices) != 1:
        raise ValueError('Default route has multiple uplinks. Specify --uplink vmbrN.')
    bridge = devices.pop()
    if not re.fullmatch(r'vmbr[0-9]+', bridge):
        raise ValueError('Default route is not directly on a vmbr bridge '
                         '(VLAN, VPN or routed setup). Specify --uplink vmbrN.')
    return bridge


def main():
    routes = json.loads(subprocess.check_output(
        ['ip', '-j', '-4', 'route', 'show', 'default'], text=True))
    print(select_bridge(routes))


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        print(f'ERROR: Cannot detect ISP uplink: {error}', file=sys.stderr)
        sys.exit(1)
