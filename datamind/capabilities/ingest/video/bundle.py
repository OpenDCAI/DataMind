"""Profile-scoped immutable video evidence, shared by KB and Graph."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
from pathlib import Path

from filelock import AsyncFileLock

from datamind.core.errors import CapabilityError, ConfigError
from datamind.core.protocols import GraphEvidence, GraphTriple
from . import PIPELINE_VERSION
from .frames import extract_frames, probe
from .providers import describe_frame, transcribe


def sha_file(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def recipe(cfg) -> dict:
    return {"pipeline": PIPELINE_VERSION,
            **cfg.model_dump(mode="json", exclude={"vision": {"api_key"}})}


def bundle_key(item: dict, cfg) -> str:
    value = [item["uri"], item["version"], recipe(cfg)]
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def bundle_path(root: Path, bundle_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{64}", bundle_id):
        raise CapabilityError("video", "Invalid video bundle ID")
    path = root / "kb_media" / bundle_id
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise CapabilityError("video", "Video bundle escapes the profile")
    return path


def load_bundle(root: Path, bundle_id: str, *, verify_integrity=True) -> dict:
    path = bundle_path(root, bundle_id)
    value = json.loads((path / "bundle.json").read_text(encoding="utf-8"))
    if value.get("bundle_id") != bundle_id or value.get("status") != "complete":
        raise CapabilityError("video", "Video bundle is incomplete")
    for frame in value["frames"]:
        image = path / frame["image"]
        if image.is_symlink() or image.parent != path:
            raise CapabilityError("video", "Video frame escapes its bundle")
        if verify_integrity and sha_file(image) != frame["sha256"]:
            raise CapabilityError("video", "Video frame integrity check failed")
    return value


def align_speech(frames: list[dict], segments: list[dict]) -> None:
    """Align words to slide onset (burst start), retaining original segments."""
    for frame in frames:
        frame["speech"] = []
    for segment in segments:
        start, end = segment["start_s"], segment["end_s"]
        if not all(math.isfinite(t) for t in (start, end)) or end < start or start < 0:
            raise CapabilityError("video", "Invalid ASR segment timestamps")
        words = segment.get("words") or []
        for word in words:
            if not all(math.isfinite(word[k]) for k in ('start_s', 'end_s')) or word['end_s'] < word['start_s']:
                raise CapabilityError('video', 'Invalid ASR word timestamps')
            midpoint = (word['start_s'] + word['end_s']) / 2
            if not 0 <= midpoint < frames[-1]['end_s']:
                raise CapabilityError('video', 'ASR word falls outside the video timeline')
        for frame in frames:
            if words:
                matched = [w for w in words if frame["start_s"] <= (w["start_s"] + w["end_s"]) / 2 < frame["end_s"]]
                text = "".join(w["text"] for w in matched)
                method = "word_midpoint"
            else:
                text = segment["text"] if start < frame["end_s"] and end > frame["start_s"] else ""
                method = "segment_overlap"
            if text.strip():
                frame["speech"].append({"segment_id": segment["segment_id"], "text": text,
                                        "alignment": method, "start_s": start, "end_s": end})
        segment["frame_ids"] = [f["frame_id"] for f in frames
                                if any(s["segment_id"] == segment["segment_id"] for s in f["speech"])]
        if segment['text'].strip() and not segment['frame_ids']:
            raise CapabilityError('video', 'ASR segment cannot be aligned to video frames')


async def prepare_video(root: Path, path: Path, item: dict, cfg, *, verify_source=True) -> dict:
    """Cache only complete bundles; failed/cancelled builds never publish one."""
    from datamind.core.model_clients import build_model_client
    from datamind.workspace_native import blocking

    key = bundle_key(item, cfg)
    target = bundle_path(root, key)
    if path.stat().st_size > cfg.max_bytes:
        raise CapabilityError('video', 'MP4 exceeds video.max_bytes')
    target.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncFileLock(str(target.parent / (key + ".lock")), timeout=cfg.timeout_s):
        if target.is_dir():
            os.utime(target, None)
            return await blocking(load_bundle, root, key, verify_integrity=verify_source)
        if cfg.ocr_enabled and cfg.vision is None:
            raise ConfigError("MP4 requires video.vision (vision-capable model, endpoint and credentials)")
        temporary = Path(tempfile.mkdtemp(prefix=".video-", dir=target.parent))
        client = None
        try:
            # Freeze bytes before providers; standard workspaces also check for
            # concurrent source changes. DataSpace inputs are immutable by contract.
            frozen = temporary / "source.mp4"
            await blocking(shutil.copyfile, path, frozen)
            if verify_source and "sha256:" + await blocking(sha_file, frozen) != item["version"]:
                raise CapabilityError("video", "Source changed during video preparation; retry")
            info = await blocking(probe, frozen, cfg)
            if cfg.asr_enabled and info["has_audio"] and (cfg.asr_model_dir is None or not cfg.asr_model_dir.is_dir()):
                raise ConfigError("MP4 audio requires video.asr_model_dir with local Whisper weights")
            frames = await blocking(extract_frames, frozen, temporary, cfg, info)
            client = build_model_client(cfg.vision) if cfg.ocr_enabled else None
            semaphore = asyncio.Semaphore(cfg.ocr_concurrency)

            async def ocr(frame):
                async with semaphore:
                    if cfg.ocr_enabled:
                        frame.update(await describe_frame(client, temporary / frame["image"], cfg))
                        frame["ocr_status"] = "complete"
                    else:
                        frame.update(ocr_status="disabled", layout=None, ocr_text="")
                    frame["sha256"] = await blocking(sha_file, temporary / frame["image"])

            # TaskGroup cancels siblings on failure before the client/temp files close.
            async with asyncio.TaskGroup() as group:
                for frame in frames:
                    group.create_task(ocr(frame))
            speech = (await blocking(transcribe, frozen, cfg) if info["has_audio"] and cfg.asr_enabled
                      else {"status": "no_audio" if not info["has_audio"] else "disabled", "language": None, "segments": []})
            align_speech(frames, speech["segments"])
            value = {"bundle_id": key, "status": "complete", "source": item["uri"],
                     "source_version": item["version"], "pipeline_version": PIPELINE_VERSION,
                     "media": info, "frames": frames, "asr": speech,
                     "coverage": "packet bursts plus periodic samples; not exhaustive video understanding"}
            from datamind.workspace import _atomic_json
            _atomic_json(temporary / "bundle.json", value)
            frozen.unlink()
            os.replace(temporary, target)
            return value
        finally:
            try:
                if client is not None:
                    await client.aclose()
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary)


def text_records(bundle: dict, retrieval) -> list[dict]:
    from datamind.capabilities.kb.indexer import _split_text

    records = []
    for frame in bundle["frames"]:
        sections = [("ocr", frame["ocr_text"] + "\nLayout: " + json.dumps(frame["layout"], ensure_ascii=False))]
        sections.extend(("asr", s["text"]) for s in frame["speech"])
        for kind, text in sections:
            if not text.strip():
                continue
            prefix = f"Video {Path(bundle['source']).name}, {frame['frame_id']}, {frame['start_s']:.3f}-{frame['end_s']:.3f}s ({kind})\n"
            for part in _split_text(text, chunk_size=retrieval.chunk_size, chunk_overlap=retrieval.chunk_overlap):
                records.append({"text": prefix + part, "metadata": {
                    "media_type": "video/mp4", "kind": kind, "bundle_id": bundle["bundle_id"],
                    "frame_id": frame["frame_id"], "start_s": frame["start_s"],
                    "end_s": frame["end_s"], "capture_s": frame["capture_s"],
                    "image_sha256": frame["sha256"],
                }})
    return records


def graph_records(bundle: dict, semantics: list[dict]) -> list[dict]:
    """Deterministic media lineage plus source-validated Codex assertions."""
    source = bundle["source"]
    source_id = "file:" + hashlib.sha256(source.encode()).hexdigest()
    frames = {f["frame_id"]: f for f in bundle["frames"]}
    segments = {s["segment_id"]: s for s in bundle["asr"]["segments"]}
    prefix = "video:" + bundle["bundle_id"] + "#"

    def evidence(locator, method="explicit"):
        return GraphEvidence(source_id=source_id, source_version=bundle["source_version"],
                             locator={"uri": source, "bundle_id": bundle["bundle_id"], **locator},
                             method=method, pipeline_version=PIPELINE_VERSION)

    def frame_locator(frame):
        return {k: frame[k] for k in ("frame_id", "start_s", "end_s", "capture_s")}

    triples = []
    for frame in frames.values():
        node = prefix + frame["frame_id"]
        for subject, relation, obj, src_type, dst_type in (
            (source_id, "has_frame", node, "video", "video_frame"),
            (node, "has_layout", node + ":layout", "video_frame", "ocr_layout"),
        ):
            triples.append(GraphTriple(subject=subject, relation=relation, object=obj, source=source,
                           subject_type=src_type, object_type=dst_type, evidence=[evidence(frame_locator(frame))]))
        for speech in frame["speech"]:
            triples.append(GraphTriple(subject=node, relation="aligned_speech", object=prefix + speech["segment_id"],
                           subject_type="video_frame", object_type="speech", source=source,
                           properties={"alignment": speech["alignment"]},
                           evidence=[evidence({**frame_locator(frame), "segment_id": speech["segment_id"]})]))
    for value in semantics:
        triple = GraphTriple(**value)
        if triple.properties.get('video_bundle') != bundle['bundle_id']:
            raise ConfigError('Video semantic triples must identify the prepared properties.video_bundle')
        refs = triple.properties.get("video_evidence")
        if not isinstance(refs, list) or not refs:
            raise ConfigError("Video semantic triples require properties.video_evidence with frame_id or segment_id")
        supports = []
        for ref in refs:
            if not isinstance(ref, dict):
                raise ConfigError("Invalid video evidence locator")
            if set(ref) == {"frame_id"} and ref["frame_id"] in frames:
                locator = frame_locator(frames[ref["frame_id"]])
            elif set(ref) == {"segment_id"} and ref["segment_id"] in segments:
                segment = segments[ref["segment_id"]]
                locator = {k: segment[k] for k in ("segment_id", "start_s", "end_s", "frame_ids")}
            else:
                raise ConfigError("Video evidence must reference an existing frame or speech segment")
            supports.append(evidence(locator, "extracted"))
        triple.source, triple.evidence = source, supports
        triples.append(triple)
    return [t.model_dump(mode="json") for t in triples]
