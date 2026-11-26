"""Collect browser frame timings and actual CLI payload sizes for both workflows."""
import argparse
from collections import defaultdict
from hashlib import sha256
import json
from pathlib import Path
import platform
import shutil
from statistics import median
import subprocess
import sys
from time import perf_counter

from pugc_toolkit.placement_geometry import terrain_path
from pugc_toolkit.rendering import browser_page, Renderer
from pugc_toolkit.scene import map_scene
from pugc_toolkit.web_service import SceneStore, background_server

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "examples/ugc_route/demo.py"
SOURCE = ROOT / "tests/fixtures/blender"


def timing(values):
    values = sorted(values)
    return {"samples": values, "median": median(values), "p95": values[max(0, (95 * len(values) + 99) // 100 - 1)]}


def browser(args, maps, island):
    store = SceneStore()
    cold, warm, frame, draw, released = defaultdict(list), defaultdict(list), defaultdict(list), {}, defaultdict(list)
    data = {name: map_scene(path, args.meshes, island=island) for name, path in maps.items()}
    urls = {name: store.publish(scene, name) for name, scene in data.items()}
    with background_server(store) as endpoint:
        for index in range(args.repeats):
            with browser_page(chromium=args.chromium) as page:
                render = Renderer(page, endpoint)
                for name, url in urls.items():
                    for turn in range(2):
                        path = args.work / f"{name}.png"
                        stats = render.frame(url, path, resolution=(960, 540))
                        (cold if turn == 0 else warm)[name].append(stats["first_frame_ms"])
                    draw[name] = {key: stats[key] for key in ("draw_calls", "triangles", "geometries", "textures")}
                    frame[name].extend(page.evaluate("() => window.pugcMeasure({frames: 16})"))
                    released[name].append(page.evaluate("() => window.pugcClose()"))
    return {name: {"first_frame_cold_ms": timing(cold[name]), "first_frame_warm_ms": timing(warm[name]),
                   "interaction_frame_ms": timing(frame[name]), "render": draw[name],
                   "released": released[name]} for name in maps}


def run_demo(args, tokenizer, scenario, variant, index):
    output = args.work / f"{scenario}-{variant}-{index}"
    output.mkdir()
    command = [sys.executable, "-IB", DEMO, scenario, "--work", output, "--meshes", args.meshes]
    if args.chromium:
        command.extend(("--chromium", args.chromium))
    if args.base_level:
        command.extend(("--base-level", args.base_level))
    overhead = None
    if variant == "full":
        start = perf_counter()
        # Simulate supplying the full decoded scene instead of a filtered inspect response.
        target = SOURCE / "wrong-tower.pugc" if scenario == "tower" else None
        if target:
            manifest = map_scene(target, args.meshes).manifest
        else:
            temporary = output / "empty.json"
            from pugc_toolkit.ugc_project import new_game_project, save_json

            save_json(temporary, new_game_project("Gate passage 600 cm"))
            manifest = map_scene(temporary, args.meshes).manifest
        payload = json.dumps(manifest, ensure_ascii=False).encode()
        overhead = {"value": payload, "seconds": perf_counter() - start}
    done = subprocess.run(command, capture_output=True, check=True, text=True, timeout=240)
    result = json.loads(done.stdout)
    trace = json.loads((output / "trace.json").read_bytes())
    payloads = [json.dumps(row["response"], ensure_ascii=True).encode() + b"\n" for row in trace]
    calls, seconds, actual_bytes = result["calls"], result["seconds"], result["stdout_bytes"]
    if overhead:
        if scenario == "tower":
            index_inspect = next(i for i, row in enumerate(trace) if row["arguments"][0] == "inspect")
            actual_bytes -= trace[index_inspect]["stdout_bytes"]
            seconds -= trace[index_inspect]["seconds"]
            payloads[index_inspect] = overhead["value"]
        else:
            calls += 1
            payloads.append(overhead["value"])
        actual_bytes += len(overhead["value"])
        seconds += overhead["seconds"]
    return {"calls": calls, "stdout_bytes": actual_bytes,
            "tokens": sum(len(tokenizer.encode(value.decode(), disallowed_special=())) for value in payloads),
            "builds": result["builds"], "iterations": 1, "seconds": seconds, "result": result["data"]}, output


def main():
    import tiktoken
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--meshes", type=Path, required=True)
    parser.add_argument("--base-level", type=Path)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    args.work.mkdir(parents=True, exist_ok=True)
    tok = tiktoken.get_encoding("cl100k_base")
    batches = defaultdict(dict)
    maps = {}
    for scenario in ("gate", "tower"):
        for variant in ("filtered", "full"):
            rows = []
            for index in range(args.repeats):
                row, output = run_demo(args, tok, scenario, variant, index)
                rows.append(row)
                if scenario not in maps:
                    maps[scenario] = output / ("gate.pugc" if scenario == "gate" else "corrected.pugc")
            batches[scenario][variant] = {"runs": rows, "seconds": timing([row["seconds"] for row in rows])}
    island = terrain_path(args.base_level)
    metadata = {"platform": platform.platform(), "python": platform.python_version(),
                "processor": platform.processor(), "tokenizer": "cl100k_base / tiktoken 0.12.0",
                "island_sha256": sha256(island.read_bytes()).hexdigest(),
                "browser": subprocess.check_output([args.chromium or shutil.which("chromium"), "--version"], text=True).strip()
                if args.chromium or shutil.which("chromium") else "Chromium bundled by Playwright",
                "full_variant": "Same build and verification with full manifest payload in place of scoped inspect (tower) "
                                "or in addition to author operations (gate)."}
    output = {"environment": metadata, "cli": batches, "browser": browser(args, maps, island)}
    (args.work / "benchmark.json").write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
