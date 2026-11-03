# MP4 workspace evidence

MP4 sources join the existing Workspace KB and Graph publication pipeline.
They do not introduce another data surface. Preprocessing produces immutable
PNG frames, JSON Hiccup layout/OCR, word-timestamped speech and alignment data.
KB embeds text and stores the bundle/frame locators; original PNGs live under
`storage/<profile>/workspace/kb_media/<bundle_id>/`. KB image embeddings are
not required. `kb_video_frame` returns real PNG image content over MCP.

## Setup

Install the optional video dependencies in your DataMind environment:

```bash
python -m pip install -e '.[video]'
```

Run this command from the repository root after activating your virtual or
Conda environment. Install ffmpeg/ffprobe separately as system executables.

Local GPU hardware is not required: PyAV decodes frames, and Whisper runs on
CPU with int8 computation. Vision OCR calls the configured API. Hosting a large
vision model locally would usually require a GPU; the current ASR adapter does
not automatically switch devices when a GPU is present.

These dependencies are lazy: text,
table and graph tools do not import Whisper, PyAV or call vision models.

Download ASR weights explicitly, for example:

```bash
python scripts/download_video_models.py --output /path/to/models/faster-whisper
```

`tiny` detects Chinese/English when `video.language_model_dir` is set; `medium`
transcribes on CPU/int8 with word timestamps and VAD. If the tiny path is
omitted, medium detects the language itself. Set `video.language` to override.
No model is downloaded during ingestion. Videos without an audio stream are
marked `no_audio`; silent audio can legitimately return zero speech segments.

Copy the relevant settings from `.env.video.example` into your configuration,
filling the empty vision API URL/key first. The model must accept images via
OpenAI-compatible chat completions or Anthropic messages. Configure embeddings
and the main Codex host as usual; video vision credentials are separate.
The example does not enable a working API endpoint and is not loaded automatically.

## Ingest and retrieve

1. In a write operation, call `workspace_prepare(path)` for a task-local source
   directory. MP4 files appear in its SHA-256 inventory.
2. Call `video_prepare(path_to_mp4)`. Repeated calls reuse a complete bundle for
   the same source bytes and processing configuration. Preparation alone does
   not publish KB/Graph results.
3. Page through `kb_video_read(bundle_id, offset, limit)` until `next_offset` is
   null. View relevant images using `kb_video_frame(bundle_id, frame_id)`.
4. Codex extracts semantic triples using the images, layout and speech. Each
   triple needs `properties.video_bundle` set to the prepared bundle ID and
   `properties.video_evidence`, for example:

   ```json
   {"subject":"Revenue","relation":"amount","object":"42 million",
    "properties":{"video_bundle":"<bundle_id from video_prepare>","video_evidence":[{"frame_id":"frame-0001"},{"segment_id":"speech-0000"}]}}
   ```

5. Include the MP4 `{uri, version, triples}` entry in `workspace_publish` along
   with other document assertions. Publish with `surfaces=["kb","graph","db"]`
   (or the required subset). Use explicit empty triples if no semantic
   relationships exist. A preprocessing failure is not an empty source.
6. Start a pinned read operation. `kb_search` returns `bundle_id`, `frame_id`,
   timestamps, source version and PNG checksum in chunk metadata. The video
   read tools fetch layout/speech or original PNGs for the same revision.

Text evidence reads load the bundle manifest without scanning frame images.
Ordinary `kb_video_frame` reads only the selected PNG and verifies those exact
bytes before returning them; it does not hash every other frame. DataSpace
evaluation trusts immutable inputs and prepared media, skipping repeated source
and image checksum comparisons. Bundle completeness, task membership, safe image
paths and the image size limit still apply. First-build fingerprints remain in
metadata; this does not enable automatic image viewing.

The Python `Workspace.update` API follows the same contract and accepts
`graph_sources`. For MP4 Graph publication, it requires explicit assertions;
it does not create a second conversational agent. Snapshot mode needs entries
for all documents; incremental mode can reuse unchanged source assertions.
When changing video processing configuration, prepare again and resubmit video
assertions. Legacy live `kb_add_file`, `kb_add_path` and CocoIndex imports remain
text/document pipelines; use Workspace publication for MP4.

## Evidence and lifecycle

Graph includes deterministic `has_frame`, `has_layout` and `aligned_speech`
edges. Semantic triples receive validated frame/speech locators, source hash
and pipeline version. Multiple frames can support the same assertion.
Relation vocabulary and semantic triple limits do not suppress structural edges.
Frame alignment uses slide onset (`start_s`); PNG evidence records the actual
decoded frame time (`capture_s`). Words are assigned by midpoint. Missing word
timestamps use explicit segment overlap and may refer to more than one frame.

Both snapshot and incremental storage persist bundle references in their
manifests/checkpoints. Changed/deleted sources replace the current revision's
KB and Graph data; retained revisions, leases, rollback and migration preserve
their old media. GC protects retained bundle references. Unreferenced media
has a 24-hour grace period for preparation before publication; expired bundles
and their stale snapshot checkpoints are eligible for cleanup.

The cache key includes source URI/hash, pipeline and nonsecret configuration.
Increment `video.cache_revision` when replacing model weights or changing a
server-side model behind the same name. Credentials are neither written into
bundles nor hashed as cache content. Failed preprocessing leaves no complete
bundle, and failed publication leaves the previous Workspace head readable.

## Method and limits

The design follows [zhezh/kddcup2026_champion](https://github.com/zhezh/kddcup2026_champion)
at commit `bdc874fc4260e3565ae0dce041728fdf5b376709`: packet-size bursts,
settled-slide frames, Hiccup layout OCR, local tiny/medium Whisper and timeline
alignment. Its repository contained no license file when reviewed. DataMind's
adapter is independently implemented; no champion source files are vendored.
Its private gateways, static-ffmpeg downloader, remote ASR service and
question-specific voting/answer adjudication are not imported. Hiccup is
represented as validated JSON arrays, avoiding executable parsing.

Packet-burst detection is a heuristic for presentation videos. Periodic guard
frames improve coverage of hard cuts but do not guarantee capturing every
visual event. Full-resolution PNGs remain available for visual verification.
ASR and OCR may be wrong; no accuracy claim follows from a successful import.
Limits on duration, bytes and frame count fail explicitly rather than silently
dropping evidence. OCR token truncation, malformed layout, missing models and
image checksum failures also fail explicitly in ordinary mode. Vision request retries/timeouts
use the existing model client; offline CPU transcription has no API dependency.

HTTP upload's existing 25 MiB limit is unchanged. For larger videos, place the
file in an allowed server-side workspace directory. MP4 support does not change
SQLite workspace input support, SQL row limits or existing column normalization.


## Direct question answering and OCR format

`video_prepare(path, ocr=True, asr=True)` can be used directly without publishing
KB or Graph. `ocr=False` and/or `asr=False` skip only that explicitly selected
operation; manifests report `disabled`. Frame image extraction and direct
`kb_video_frame` viewing remain available, including without auxiliary models.
The flags participate in the bundle cache identity. A failed enabled operation
still fails rather than publishing a silently partial bundle.

`video.ocr_response_format` supports the original `hiccup`, opt-in `json_schema`
for providers that enforce it, and `text`. In `text` mode, the model
transcribes text and Python constructs/serializes the Hiccup wrapper. This
removes model-generated JSON syntax/schema failures, but provides no guarantee
of OCR accuracy or structured page layout. Inspect original PNGs for charts,
layout and verification. Refusals/empty responses, truncation and transport
failures remain explicit errors. Select `json_schema` only when your provider
supports and enforces the requested schema; enforcement is provider-dependent.
