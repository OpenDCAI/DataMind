"""Video preparation and bounded KB evidence reads for Python and MCP callers."""
from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from datamind.core.errors import CapabilityError
from datamind.core.tools import ToolSpec
from datamind.capabilities.ingest.video.bundle import bundle_path, load_bundle, prepare_video, sha_file


def video_tools(settings, *, root=None, allowed_bundles=None, verify_integrity=None):
    """A pinned reader supplies its exact bundle allowlist, including an empty set."""
    if verify_integrity is None:
        verify_integrity = settings is None or settings.workspace.input_policy != 'dataspace'
    def media_root():
        return root if root is not None else (settings.data.storage_dir / "workspace").resolve()

    def check(bundle_id):
        if allowed_bundles is not None and bundle_id not in allowed_bundles:
            raise CapabilityError("video", "Video evidence must belong to the pinned revision")
        # Text reads need only the manifest. Image reads verify the returned bytes.
        return load_bundle(media_root(), bundle_id, verify_integrity=False)

    async def prepare(path, ocr=True, asr=True):
        from datamind.capabilities.ingest import build_ingest_service
        from datamind.capabilities.ingest.service import _resolve_safe_path
        from datamind.workspace_native import blocking
        service = build_ingest_service(settings=settings, kb=None, db=None, graph=None, llm_client=None)
        source = _resolve_safe_path(path, service._allowed_roots)
        if source.suffix.lower() != ".mp4" or not source.is_file():
            raise CapabilityError("video", "video_prepare requires an MP4 file")
        item = {"uri": str(source), "version": "sha256:" + await blocking(sha_file, source)}
        from filelock import AsyncFileLock
        root = media_root()
        root.mkdir(parents=True, exist_ok=True)
        # Use the same lock order as incremental publication and GC. The
        # post-preparation grace period protects the gap before publication.
        async with AsyncFileLock(str(root / 'writer.lock'), timeout=settings.video.timeout_s):
            cfg = settings.video.model_copy(update={"ocr_enabled": ocr, "asr_enabled": asr})
            bundle = await prepare_video(root, source, item, cfg, verify_source=verify_integrity)
        return {"bundle_id": bundle["bundle_id"], "source": bundle["source"],
                "source_version": bundle["source_version"], "frames": len(bundle["frames"]),
                "asr_status": bundle["asr"]["status"], "ocr_status": "complete" if ocr else "disabled", "coverage": bundle["coverage"],
                "instructions": "Read kb_video_read and view kb_video_frame images directly to answer questions. "
                "OCR and ASR are optional evidence; disabled does not mean no text or speech exists. "
                "No KB/Graph/DB build or workspace publication is required. "
                "If indexing or a versioned publication is useful, use the existing ingestion tools and video evidence locators."}


    async def read(bundle_id, offset=0, limit=10):
        from datamind.workspace_native import blocking
        if offset < 0 or not 1 <= limit <= 25:
            raise CapabilityError("video", "Invalid video evidence page")
        bundle = await blocking(check, bundle_id)
        frames = bundle["frames"][offset:offset + limit]
        segments = {s["segment_id"] for f in frames for s in f["speech"]}
        end = offset + len(frames)
        return {"bundle_id": bundle_id, "source": bundle["source"], "source_version": bundle["source_version"],
                "media": bundle["media"], "coverage": bundle["coverage"], "frames": frames,
                "asr_status": bundle["asr"]["status"],
                "segments": [s for s in bundle["asr"]["segments"] if s["segment_id"] in segments],
                "next_offset": end if end < len(bundle["frames"]) else None, "total_frames": len(bundle["frames"])}

    async def frame(bundle_id, frame_id):
        from datamind.workspace_native import blocking
        bundle = await blocking(check, bundle_id)
        item = next((f for f in bundle["frames"] if f["frame_id"] == frame_id), None)
        if item is None:
            raise CapabilityError("video", "Unknown video frame")
        image = bundle_path(media_root(), bundle_id) / item["image"]
        if image.stat().st_size > 20 * 1024 * 1024:
            raise CapabilityError("video", "Frame exceeds the 20 MiB image tool limit")
        def read_image():
            data = image.read_bytes()
            if len(data) > 20 * 1024 * 1024:
                raise CapabilityError("video", "Frame exceeds the 20 MiB image tool limit")
            if verify_integrity and hashlib.sha256(data).hexdigest() != item['sha256']:
                raise CapabilityError("video", "Video frame integrity check failed")
            return base64.b64encode(data).decode('ascii')
        data = await blocking(read_image)
        return {"bundle_id": bundle_id, "frame_id": frame_id, "capture_s": item["capture_s"],
                "image_sha256": item['sha256'],
                "source": bundle["source"], "source_version": bundle["source_version"],
                "image": {"type": "image", "mimeType": "image/png",
                          "data": data}}

    base = {"bundle_id": {"type": "string", "pattern": "^[a-f0-9]{64}$"}}
    return [
        ToolSpec("video_prepare", "Extract MP4 frames for direct viewing. Optionally run OCR and/or local ASR; ocr=false/asr=false needs no auxiliary models. Does not require workspace publication.",
                 {"type": "object", "properties": {"path": {"type": "string"}, "ocr": {"type": "boolean", "default": True}, "asr": {"type": "boolean", "default": True}}, "required": ["path"]},
                 prepare, {"access": "write", "workspace_management": True}),
        ToolSpec("kb_video_read", "Read a page of managed video frames, OCR/layout and aligned speech; follow next_offset to completion.",
                 {"type": "object", "properties": {**base, "offset": {"type": "integer", "minimum": 0},
                  "limit": {"type": "integer", "minimum": 1, "maximum": 25}}, "required": ["bundle_id"]},
                 read, {"access": "read"}),
        ToolSpec("kb_video_frame", "View the original PNG for a KB video frame. Returns MCP image content, not just a filename.",
                 {"type": "object", "properties": {**base, "frame_id": {"type": "string"}},
                  "required": ["bundle_id", "frame_id"]}, frame, {"access": "read"}),
    ]


def mcp_content(value: dict) -> dict:
    """Move PNG bytes out of structured JSON into the standard MCP image block."""
    import json
    result = value.get("result", {})
    image = result.get("image") if isinstance(result, dict) else None
    structured = value
    if image is not None:
        structured = {**value, "result": {k: v for k, v in result.items() if k != "image"}}
    content = [{"type": "text", "text": json.dumps(structured, ensure_ascii=False, default=str)}]
    if image is not None:
        content.append(image)
    return {"content": content, "structuredContent": structured}
