#!/usr/bin/env python3
"""Worked admission examples. No credentials, network, or live cache are used."""
import importlib.util
import os
from pathlib import Path
import tempfile

SCRIPT = os.environ.get('USAGE_UNDER_TEST', str(Path(__file__).resolve().parents[1] / 'scripts/check-usage.py'))

def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ['CLAUDE_CONFIG_DIR'] = tmp
        os.environ['CLAUDE_USAGE_CACHE'] = str(Path(tmp) / 'cache.json')
        spec = importlib.util.spec_from_file_location('usage_examples', SCRIPT)
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        cases = [
            ('unused capacity halfway through week', {'kind':'weekly_all','percent':10,'resets_in_seconds':302400}, 'accelerate'),
            ('on target halfway through week', {'kind':'weekly_all','percent':50,'resets_in_seconds':302400}, 'cruise'),
            ('slightly ahead of target', {'kind':'weekly_all','percent':51,'resets_in_seconds':302400}, 'coast'),
            ('far ahead of target', {'kind':'weekly_all','percent':60,'resets_in_seconds':302400}, 'brake'),
            ('exhausted', {'kind':'session','percent':100,'resets_in_seconds':7200}, 'park'),
            ('five minute checkpoint reserve', {'kind':'session','percent':90,'resets_in_seconds':9000,'burn':{'delta_percent':40,'over_seconds':600}}, 'park'),
            ('unknown future meter', {'kind':'future_pool','percent':10,'resets_in_seconds':7200}, 'cruise'),
            ('fresh week is not spare capacity', {'kind':'weekly_all','percent':0,'resets_in_seconds':604799}, 'cruise'),
            ('near-reset spare capacity', {'kind':'weekly_all','percent':90,'resets_in_seconds':600}, 'accelerate'),
        ]
        for name, limit, expected in cases:
            got, reason = m.limit_posture(limit)
            assert got == expected, (name, expected, got, reason)
            assert reason, name
            print(f'{name}: {got}')
        print(f'{len(cases)} worked examples passed')

if __name__ == '__main__':
    main()
