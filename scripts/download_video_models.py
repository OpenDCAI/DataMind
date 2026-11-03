"""Explicitly download offline ASR weights; never called during ingestion."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    from huggingface_hub import HfApi, snapshot_download

    api = HfApi()
    records = []
    for name in ('tiny', 'medium'):
        repository = 'Systran/faster-whisper-' + name
        info = api.model_info(repository, files_metadata=True)
        path = Path(snapshot_download(
            repo_id=repository, revision=info.sha, local_dir=str(args.output / name),
            allow_patterns=['config.json', 'model.bin', 'tokenizer.json', 'vocabulary.*', 'preprocessor_config.json'],
            max_workers=3,
        ))
        files = {}
        for source in path.iterdir():
            if source.is_file():
                sha = hashlib.sha256()
                with source.open('rb') as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b''):
                        sha.update(block)
                files[source.name] = sha.hexdigest()
        weight = next(s for s in info.siblings if s.rfilename == 'model.bin')
        if weight.lfs and files.get('model.bin') != weight.lfs.sha256:
            raise RuntimeError(f'{repository}: model.bin checksum mismatch')
        records.append({'repository': repository, 'revision': info.sha, 'path': str(path.resolve()), 'sha256': files})
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / 'models.json').write_text(json.dumps(records, indent=2), encoding='utf-8')
    print(json.dumps(records, indent=2))


if __name__ == '__main__':
    main()
