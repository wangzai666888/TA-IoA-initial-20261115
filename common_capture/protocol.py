"""Read-only checks for the independent capture-v1 experiment snapshot."""
import hashlib
import json
from pathlib import Path

from .rules import frozen_rules
from .scenario import load_scene

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL = ROOT / 'experiments/capture_v1/protocol.json'


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def validate_protocol(path=PROTOCOL):
    protocol = json.loads(Path(path).read_text(encoding='utf-8'))
    if protocol.get('protocol_id') != 'capture_shared_v1' or protocol.get('status') != 'boundary_frozen':
        raise ValueError('unknown capture protocol')
    if protocol.get('effective_rules') != frozen_rules():
        raise ValueError('capture rules differ from frozen protocol')
    manifest_path = ROOT / 'experiments/capture_v1/source_manifest.json'
    if sha256(manifest_path) != protocol['source_manifest_sha256']:
        raise ValueError('source scene manifest differs from frozen protocol')
    source_manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    listed = {row['file']: row for row in source_manifest['scenes']}
    copied = ROOT / 'experiments/capture_v1/scenes'
    if {p.name for p in copied.glob('*.json')} != set(protocol['scenes']):
        raise ValueError('frozen capture scene set differs')
    if set(listed) != set(protocol['scenes']):
        raise ValueError('source manifest scene set differs')
    for filename, expected in protocol['scenes'].items():
        scene_path = copied / filename
        if (sha256(scene_path) != expected['sha256']
                or expected['sha256'] != listed[filename]['scenario_sha256']):
            raise ValueError('frozen capture scene bytes differ: ' + filename)
        scene = load_scene(scene_path)
        if scene.scenario_id != expected['scenario_id']:
            raise ValueError('frozen capture scene ID differs: ' + filename)
    for relative, expected in protocol['shared_source_sha256'].items():
        if sha256(ROOT / relative) != expected:
            raise ValueError('capture shared source differs: ' + relative)
    return {'protocol_id': protocol['protocol_id'], 'scene_count': len(protocol['scenes']),
            'shared_sources_valid': True, 'scenario_bytes_valid': True,
            'methods_connected': False}


def load_frozen_scene(filename, path=PROTOCOL):
    """Accept only a named, hash-matched scene from this protocol."""
    protocol = json.loads(Path(path).read_text(encoding='utf-8'))
    if filename not in protocol['scenes'] or Path(filename).name != filename:
        raise ValueError('unknown frozen capture scene')
    scene_path = ROOT / 'experiments/capture_v1/scenes' / filename
    if sha256(scene_path) != protocol['scenes'][filename]['sha256']:
        raise ValueError('frozen capture scene bytes differ: ' + filename)
    return load_scene(scene_path)


if __name__ == '__main__':
    print(json.dumps(validate_protocol(), indent=2))
