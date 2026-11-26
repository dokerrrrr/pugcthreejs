"""Camera settings and timed paths in game centimetres."""
from bisect import bisect_right
from math import ceil, isfinite
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

import numpy as np

from . import cli_protocol as cli
from .rendering import renderer
from .scene import game_camera
from .ugc_project import load_json
from .ugc_storage import install_staged, lock_outputs, output_paths
from .ugc_validation import finite

FIELDS = {"eye_cm", "target_cm", "fov_deg", "roll_deg", "clip_cm", "ortho_height_cm"}


def camera_record(pose, size):
    if not isinstance(pose, dict) or pose.keys() - FIELDS:
        raise ValueError("Expected camera fields eye_cm, target_cm, fov_deg, roll_deg, clip_cm or ortho_height_cm")
    for key in ("eye_cm", "target_cm"):
        value = pose.get(key)
        if not isinstance(value, (list, tuple)) or len(value) != 3 or not all(map(finite, value)):
            raise ValueError(f"{key} requires three finite game coordinates")
    fov, roll, clip, height = (pose.get(key, default) for key, default in
                               (("fov_deg", 50), ("roll_deg", 0), ("clip_cm", None), ("ortho_height_cm", None)))
    if not finite(fov) or not 0 < fov < 180 or not finite(roll):
        raise ValueError("Expected 0 < vertical fov_deg < 180 and a finite roll_deg")
    if clip is not None and (not isinstance(clip, (list, tuple)) or len(clip) != 2
                             or not all(map(finite, clip)) or not 0 < clip[0] < clip[1]):
        raise ValueError("Expected clip_cm with 0 < near < far")
    if height is not None and (not finite(height) or height <= 0 or "fov_deg" in pose):
        raise ValueError("Use a positive ortho_height_cm for orthographic projection")
    record = game_camera(pose["eye_cm"], pose["target_cm"], fov, size, roll_deg=roll,
                         clip_cm=clip, ortho_height_cm=height)
    info = {"eye_cm": pose["eye_cm"], "target_cm": pose["target_cm"], "roll_deg": roll,
            "clip_cm": list(clip) if clip is not None else [value * 100 for value in record["planes"][-2:]],
            **({"ortho_height_cm": height} if height is not None else {"fov_deg": fov})}
    return record, info


def path_settings(path, size):
    data = load_json(path)
    if not isinstance(data, dict) or data.keys() - {"fps", "keyframes", "interpolation"}:
        raise ValueError("Expected camera path fields fps, interpolation and keyframes")
    fps, interpolation, frames = data.get("fps", 30), data.get("interpolation", "linear"), data.get("keyframes")
    if type(fps) is not int or not 1 <= fps <= 120 or interpolation not in ("linear", "smooth"):
        raise ValueError("Expected integer fps from 1 to 120 and linear or smooth interpolation")
    if not isinstance(frames, list) or len(frames) < 2:
        raise ValueError("Expected at least two camera keyframes")
    times, poses = [], []
    for index, frame in enumerate(frames):
        time = frame.get("time_s") if isinstance(frame, dict) else None
        if not finite(time) or (time <= times[-1] if times else time != 0):
            raise ValueError(f"Keyframe {index}: time_s must start at zero and increase")
        _, pose = camera_record({key: value for key, value in frame.items() if key != "time_s"}, size)
        if poses and pose.keys() != poses[0].keys():
            raise ValueError("Use the same projection for the whole camera path")
        times.append(time)
        poses.append(pose)
    if not isfinite(times[-1] * fps):
        raise ValueError("Expected a finite frame count")
    return fps, interpolation, times, poses


def interpolated_pose(time, times, poses, interpolation):
    index = min(bisect_right(times, time) - 1, len(times) - 2)
    end = index + 1
    duration = times[end] - times[index]
    u = (time - times[index]) / duration
    result = {}
    for key, value in poses[index].items():
        a, b = np.asarray(value, dtype=float), np.asarray(poses[end][key], dtype=float)
        if interpolation == "smooth" and key in ("eye_cm", "target_cm"):
            prev, following = max(0, index - 1), min(end + 1, len(times) - 1)
            in_tangent = (b - poses[prev][key]) * duration / (times[end] - times[prev])
            out_tangent = (poses[following][key] - a) * duration / (times[following] - times[index])
            point = (1 - u) * a + u * b + u * (1 - u) * ((1 - u) * (in_tangent - (b - a))
                                                        - u * (out_tangent - (b - a)))
        else:
            point = (1 - u) * a + u * b
        result[key] = point.tolist()
    return result


def render_flight(args, scene, *, inputs):
    width, height = args.size
    if args.out.suffix.lower() != ".mp4" or any(n < 2 or n > 4096 or n % 2 for n in (width, height)):
        raise ValueError("Choose an MP4 output and even dimensions from 2 to 4096")
    fps, interpolation, times, poses = path_settings(args.path, args.size)
    executable = shutil.which(str(args.ffmpeg) if args.ffmpeg else "ffmpeg")
    if executable is None:
        raise cli.CliError(cli.RESOURCE, "ffmpeg_missing", "FFmpeg was not found.",
                           "Install FFmpeg with libx264 or pass --ffmpeg.", path=args.ffmpeg)
    count = ceil(times[-1] * fps) + 1
    args.out.parent.mkdir(parents=True, exist_ok=True)
    (output,) = output_paths((args.out,), inputs=(*inputs, args.path))
    with lock_outputs((output,)), TemporaryDirectory(prefix=".pugc-flight-", dir=output.parent) as temp:
        directory = Path(temp)
        with renderer(scene, chromium=args.chromium) as (render, (url,)):
            for index in range(count):
                time = min(index / fps, times[-1])
                pose = interpolated_pose(time, times, poses, interpolation)
                camera, _ = camera_record(pose, args.size)
                render.frame(url, directory / f"{index:08d}.png", camera=camera, resolution=args.size)
        staged = directory / "flight.mp4"
        process = subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin",
                                  "-framerate", str(fps), "-i", str(directory / "%08d.png"), "-c:v", "libx264",
                                  "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(staged)],
                                 capture_output=True, text=True)
        if process.returncode:
            raise cli.CliError(cli.RESOURCE, "video_encode_failed", process.stderr.strip(),
                               "Check FFmpeg and its libx264 encoder.", path=output)
        with staged.open("r+b") as stream:
            os.fsync(stream.fileno())
        install_staged(staged, output)
    return {"resolution": [width, height], "fps": fps, "frames": count, "duration_s": count / fps,
            "interpolation": interpolation, "keyframes": len(times)}, output
