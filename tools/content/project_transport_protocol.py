"""Project the Mod's v2 codec using the canonical Core schema and generator."""
import argparse
import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TARGET = ROOT / 'doom_eap/runtime/sentinel_protocol_generated.py'


def projection(core):
    generator = runpy.run_path(str(core / 'tools/generate_protocol.py'))
    rendered = generator['render']((core / 'protocol/schema.json').read_bytes())
    source = rendered['protocol/generated/sentinel_protocol.py'].decode('utf-8')
    lines = source.splitlines(keepends=True)
    lines[0] = lines[0].replace('python -B tools/generate_protocol.py',
                              'python -B tools/content/project_transport_protocol.py')
    return ''.join(lines).rstrip().encode('utf-8') + b'\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--core-root', type=Path, default=ROOT.parent / 'Sentinel-Core')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    output = projection(args.core_root)
    if args.check:
        if TARGET.read_bytes() != output:
            parser.exit(1, 'Generated transport codec drift\n')
    else:
        TARGET.write_bytes(output)


if __name__ == '__main__':
    main()
