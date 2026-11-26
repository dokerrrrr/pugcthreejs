"""Run headless visualization, the two demos and numerical checks from written files."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

import subprocess
import sys
from zipfile import ZipFile

from pugc_toolkit.scene import map_scene, reference_scene
from pugc_toolkit.three_compare import compare
from pugc_toolkit.ugc_project import load_json

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests/fixtures/blender"


def cli(*args, expected=0):
    run = subprocess.run([sys.executable, "-IB", "-m", "pugc_toolkit", "--json", *map(str, args)],
                         capture_output=True, text=True, check=False, timeout=120)
    assert run.returncode == expected, run.stdout + run.stderr
    return json.loads(run.stdout)


def visualization(args):
    gate = args.work / "gate"
    binary = gate / "gate.pugc"
    bounds = (50580, 50980, 50615, 51020)
    digest = sha256(binary.read_bytes()).hexdigest()
    area = cli("area", *bounds, "--map", binary, "--surface", "placements", "--ids", "gate-a",
               "--meshes", args.meshes, "--z-range-cm", 1300, 1400, "--samples", 8, "--grid",
               "--out", gate / "site.png", expected=4)
    data = area["data"]
    assert data["pugc_sha256"] == digest
    assert 1360 < data["min_z_cm"] <= data["max_z_cm"] < 1361
    assert 0 < data["coverage_fraction"] < 1
    assert Path(area["artifacts"]["top_view"]).is_file()

    pose = {"eye_cm": [50100, 50300, 1450], "target_cm": [50920, 51000, 1150], "fov_deg": 46,
            "roll_deg": 5, "clip_cm": [10, 200000]}
    settings = gate / "pose.json"
    settings.write_text(json.dumps(pose), encoding="utf-8")
    common = [binary, "--meshes", args.meshes, "--size", 480, 270]
    for option, value in (("--chromium", args.chromium), ("--base-level", args.base_level)):
        if value is not None:
            common.extend((option, value))
    frame = cli("preview", *common, "--camera-file", settings, "--out", gate / "camera.png")
    assert frame["data"]["camera_pose"] == pose
    assert frame["data"]["pugc_sha256"] == digest

    trajectory = gate / "flight.json"
    trajectory.write_text(json.dumps({"fps": 4, "interpolation": "smooth", "keyframes": [
        {**pose, "time_s": 0},
        {**pose, "time_s": .5, "eye_cm": [50300, 50200, 1550], "roll_deg": 1},
        {**pose, "time_s": 1, "eye_cm": [50800, 50100, 1750], "fov_deg": 55},
    ]}), encoding="utf-8")
    output = gate / "flight.mp4"
    flight = cli("flythrough", *common, "--path", trajectory, "--out", output)
    assert flight["data"]["frames"] == 5 and flight["data"]["fps"] == 4
    assert flight["data"]["pugc_sha256"] == digest
    assert output.read_bytes()[4:8] == b"ftyp"


def numeric(args):
    partial = args.work / "incomplete-meshes.zip"
    with ZipFile(args.meshes) as source, ZipFile(partial, "w") as out:
        for name in source.namelist():
            if name != "bp-mesh/16087.bin.gz":
                out.writestr(name, source.read(name))
    report = cli("audit", FIX / "overpass.pugc", "--project", FIX / "overpass.json", "--meshes", partial,
                 "--strict", "--out", args.work / "partial-audit.json", expected=4)
    assert report["data"]["geometry_check"]["status"] == "partial"
    assert {row["object_id"] for row in report["diagnostics"]} == {16087}
    cli("validate", FIX / "route.pugc", "--receipt", FIX / "broken-receipt.json", expected=1)
    project = load_json(FIX / "route.json")
    scene = map_scene(FIX / "route.pugc", args.meshes)
    source = reference_scene(FIX / "source.npz", project)
    first, _ = compare(source, scene, project, ["Approach"])
    second, _ = compare(source, scene, project, ["Alongside"])
    assert first["comparison_key"] != second["comparison_key"]
    modified_source = args.work / "source.npz"
    with ZipFile(FIX / "source.npz") as src, ZipFile(modified_source, "w") as archive:
        for name in src.namelist():
            archive.writestr(name, src.read(name))
        archive.comment = b"compare reference cache invalidation"
    next_ref = reference_scene(modified_source, project)
    third, _ = compare(next_ref, scene, project, ["Approach"])
    assert first["comparison_key"] != third["comparison_key"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work", required=True, type=Path)
    parser.add_argument("--meshes", required=True, type=Path)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--base-level", type=Path)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        launcher = args.work / "broken-chromium"
        launcher.write_text('#!/bin/sh\nexec /missing/pugc-chromium "$@"\n', encoding="utf-8")
        launcher.chmod(0o700)
        doctor = cli("doctor", "--chromium", launcher, expected=4)
        assert doctor["data"]["features"]["chromium"] is False
        assert doctor["diagnostics"][0]["code"] == "chromium_unavailable"
        assert "not found" in doctor["diagnostics"][0]["message"]
    for name in ("gate", "tower"):
        cmd = [sys.executable, "-IB", ROOT / "examples/ugc_route/demo.py", name,
               "--work", args.work / name, "--meshes", args.meshes]
        for option, value in (("--chromium", args.chromium), ("--base-level", args.base_level)):
            if value:
                cmd.extend((option, value))
        subprocess.run(cmd, check=True, timeout=240)
    numeric(args)
    visualization(args)


if __name__ == "__main__":
    main()
