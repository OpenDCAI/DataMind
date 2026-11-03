"""Slide candidates from packet bursts, with periodic coverage guard frames.

The packet-size heuristic follows zhezh/kddcup2026_champion's video pipeline.
This adapter is independently implemented for DataMind's immutable evidence
contract; it does not import that repository's services or download binaries.
"""
from __future__ import annotations

import json
import math
import shutil
import statistics
import subprocess
from pathlib import Path

from datamind.core.errors import CapabilityError


def probe(path: Path, cfg) -> dict:
    binary = shutil.which("ffprobe")
    if not binary:
        raise CapabilityError("video", "Install ffmpeg/ffprobe before importing MP4")
    if path.stat().st_size > cfg.max_bytes:
        raise CapabilityError("video", "MP4 exceeds video.max_bytes")
    result = subprocess.run(
        [binary, "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=cfg.timeout_s,
    )
    info = json.loads(result.stdout)
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    duration = float(info.get("format", {}).get("duration", 0))
    if video is None or not math.isfinite(duration) or not 0 < duration <= cfg.max_duration_s:
        raise CapabilityError("video", "MP4 has no video stream or exceeds video.max_duration_s")
    return {"duration_s": duration, "codec": video.get("codec_name"),
            "has_audio": any(s.get("codec_type") == "audio" for s in streams),
            "width": video.get("width"), "height": video.get("height")}


def candidates(packets: list[tuple[float, int, bool]], duration: float, cfg) -> list[dict]:
    """Keep opening/settled slides; do not confuse periodic I-frames with bursts."""
    packets = sorted(p for p in packets if math.isfinite(p[0]) and 0 <= p[0] < duration and p[1] > 0)
    threshold = cfg.spike_ratio * statistics.median(p[1] for p in packets) if packets else math.inf
    bursts = []
    for timestamp, size, keyframe in packets:
        if size <= threshold:
            continue
        if bursts and timestamp - bursts[-1]["end"] <= cfg.merge_gap_s:
            bursts[-1].update(end=timestamp, count=bursts[-1]["count"] + 1)
        else:
            bursts.append({"start": timestamp, "end": timestamp, "count": 1, "keyframe": keyframe})
    bursts = [b for b in bursts if b["count"] > 1 or not b["keyframe"]]
    merged = []
    for burst in bursts:
        if merged and burst["start"] - merged[-1]["end"] < cfg.min_quiet_s:
            merged[-1]["end"] = burst["end"]
        else:
            merged.append(dict(burst))
    result = [{"start_s": 0., "burst_end_s": 0., "target_s": 0., "method": "opening"}]
    for index, burst in enumerate(merged):
        boundary = merged[index + 1]["start"] if index + 1 < len(merged) else duration
        target = max(burst["end"], min(burst["end"] + cfg.settle_margin_s, boundary - 0.001))
        entry = {"start_s": burst["start"], "burst_end_s": burst["end"],
                 "target_s": target, "method": "packet_burst"}
        if burst["start"] <= 0.001:
            result[0] = entry
        else:
            result.append(entry)
    # A single-frame hard cut or a different codec may evade the burst heuristic.
    # Guard sampling is recorded as sampling, never advertised as exhaustive.
    for index in range(1, math.ceil(duration / cfg.guard_interval_s)):
        timestamp = index * cfg.guard_interval_s
        if not any(abs(f["target_s"] - timestamp) < 0.5 for f in result):
            result.append({"start_s": timestamp, "burst_end_s": timestamp,
                           "target_s": timestamp, "method": "periodic_guard"})
    result.sort(key=lambda f: f["start_s"])
    if len(result) > cfg.max_frames:
        raise CapabilityError("video", f"Detected {len(result)} frames; increase video.max_frames (no frames were dropped)")
    return result


def extract_frames(path: Path, output: Path, cfg, info: dict) -> list[dict]:
    try:
        import av
    except ImportError as exc:
        raise CapabilityError("video", "Install DataMind's [video] extra for PyAV and Whisper") from exc
    result = subprocess.run(
        [shutil.which("ffprobe"), "-v", "error", "-select_streams", "v:0", "-show_entries",
         "packet=pts_time,size,flags", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=cfg.timeout_s,
    )
    packets = []
    for p in json.loads(result.stdout).get("packets", []):
        try:
            packets.append((float(p["pts_time"]), int(p["size"]), "K" in p.get("flags", "")))
        except (KeyError, ValueError):
            continue
    frames = candidates(packets, info["duration_s"], cfg)
    output.mkdir(parents=True, exist_ok=True)
    with av.open(str(path)) as container:
        stream = container.streams.video[0]
        origin = float((stream.start_time or 0) * stream.time_base)
        for index, item in enumerate(frames):
            target = item["target_s"] + origin
            container.seek(int(target / stream.time_base), stream=stream, backward=True)
            chosen = None
            for decoded in container.decode(stream):
                if decoded.pts is None:
                    continue
                chosen = decoded
                if float(decoded.pts * stream.time_base) >= target:
                    break
            if chosen is None:
                raise CapabilityError("video", f"Cannot decode candidate at {item['target_s']} seconds")
            filename = f"frame-{index:04d}.png"
            chosen.to_image().save(output / filename)
            item.update(frame_id=f"frame-{index:04d}", image=filename,
                        capture_s=max(0., float(chosen.pts * stream.time_base) - origin),
                        end_s=frames[index + 1]["start_s"] if index + 1 < len(frames) else info["duration_s"])
    return frames
