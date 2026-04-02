from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pos.ledger_registry import build_registry_manifest  # noqa: E402


def sha256_for_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description='Genera ledger_registry_manifest.json desde pos.ledger_registry.')
    parser.add_argument('--output', required=True, help='Ruta de salida del manifest JSON.')
    parser.add_argument('--build-id', default='', help='Identificador de build para el cliente.')
    parser.add_argument('--artifact-path', default='', help='Ruta opcional del binario para calcular artifact_sha256.')
    args = parser.parse_args()

    artifact_sha256 = ''
    if args.artifact_path:
        artifact_sha256 = sha256_for_file(Path(args.artifact_path).resolve())

    manifest = build_registry_manifest(build_id=args.build_id, artifact_sha256=artifact_sha256)
    output_path = Path(args.output).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(manifest, ensure_ascii=True, indent=2, sort_keys=True), encoding='utf-8')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
