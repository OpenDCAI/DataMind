"""Verify real MP4 decoding and optional local ASR without a vision API call."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from datamind.config import VideoConfig
from datamind.capabilities.ingest.video.bundle import align_speech
from datamind.capabilities.ingest.video.frames import extract_frames, probe
from datamind.capabilities.ingest.video.providers import transcribe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('video', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--models', type=Path)
    args = parser.parse_args()
    cfg = VideoConfig()
    if args.models:
        cfg.asr_model_dir = args.models / 'medium'
        cfg.language_model_dir = args.models / 'tiny'
    info = probe(args.video, cfg)
    frames = extract_frames(args.video, args.output, cfg, info)
    speech = transcribe(args.video, cfg) if args.models and info['has_audio'] else None
    if speech is not None:
        align_speech(frames, speech['segments'])
    result = {'source': str(args.video.resolve()), 'media': info, 'frames': frames,
              'asr': speech, 'vision_api_called': False, 'workspace_published': False}
    (args.output / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'frames': len(frames), 'segments': len(speech['segments']) if speech else None,
                      'output': str(args.output.resolve()), 'vision_api_called': False}))


if __name__ == '__main__':
    main()
