"""Build a 600 cm gate or correct only the tower through the installed CLI."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import sys
from time import perf_counter

from pugc_toolkit.pugc_codec import read_game

FIXTURES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "blender"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class Demo:
    def __init__(self, args):
        self.args = args
        self.work = args.work.resolve()
        self.work.mkdir(parents=True, exist_ok=True)
        self.trace = []
        self.started = perf_counter()

    def cli(self, *args, code=0):
        command = [sys.executable, "-IB", "-m", "pugc_toolkit", "--json", *map(str, args)]
        start = perf_counter()
        process = subprocess.run(command, capture_output=True, timeout=120)
        seconds = perf_counter() - start
        try:
            data = json.loads(process.stdout)
        except ValueError as exc:
            raise RuntimeError(process.stderr.decode(errors="replace")) from exc
        self.trace.append({"arguments": [str(x) for x in args], "exit_code": process.returncode,
                           "seconds": seconds, "stdout_bytes": len(process.stdout), "response": data})
        write_json(self.work / "trace.json", self.trace)
        if process.returncode != code or data["exit_code"] != code:
            raise RuntimeError(f"{args[0]} expected {code}, received {process.returncode}: {data['diagnostics']}")
        return data

    def edit(self, project, operations, **options):
        path = self.work / "edit.json"
        revision = sha256(project.read_bytes()).hexdigest()
        write_json(path, {"schema": 1, "base_sha256": revision, "operations": operations})
        arguments = ["edit", project, path, "--meshes", self.args.meshes]
        if self.args.base_level:
            arguments += ["--base-level", self.args.base_level]
        dry = self.cli(*arguments, "--dry-run")
        actual = self.cli(*arguments)
        assert dry["data"]["result_sha256"] == actual["data"]["result_sha256"]
        return actual

    def geometry_options(self):
        options = ["--meshes", self.args.meshes]
        if self.args.base_level:
            options += ["--base-level", self.args.base_level]
        return options

    def render_options(self):
        return [*self.geometry_options(), *(["--chromium", self.args.chromium] if self.args.chromium else [])]

    def gate(self):
        project, binary = self.work / "project.json", self.work / "gate.pugc"
        terrain = self.cli("terrain", 51000, 51000, *self.geometry_options()[2:])["data"]
        assert abs(terrain["terrain_z_cm"] - 1064) < 1 and terrain["water_z_cm"] == 0
        self.cli("init", project, "--name", "Gate passage 600 cm")
        catalog = self.cli("search", "wall", "--dimensions", ".6", ".6", "3",
                           "--tolerance", ".35", "--max-weight", 1, "--limit", 12,
                           "--meshes", self.args.meshes)["data"]["results"]
        candidate = next(item for item in catalog if item["objectId"] == 12005 and item["mesh_available"])
        oid = candidate["objectId"]
        self.cli("place", project, "gate-a", oid, "--at-cm", 50600, 51000, 0, "--assembly", "Gate")
        changes = self.edit(project, [
            {"op": "seat", "id": "gate-a", "support": "terrain"},
            {"op": "place_relative", "id": "gate-b", "anchor_id": "gate-a", "objectId": oid,
             "axis": "+X", "gap_cm": 600, "support": "terrain", "assembly": "Gate"},
        ])
        self.cli("build", project, binary)
        assert self.cli("validate", binary)["data"]["valid"]
        raw = read_game(binary)["objects"]
        assert len(raw) == 2
        assert [row["spawnTransform"] for row in raw] == [
            row["spawnTransform"] for row in changes["data"]["measurements"]]
        measured = self.cli("measure-gap", binary, "gate-a", "gate-b", "--expected-cm", 600,
                            "--support", "terrain", "--out", self.work / "measurement.json",
                            *self.geometry_options())
        assert measured["data"]["fits"]
        if not self.args.no_render:
            rendered = self.cli("preview", binary, "--out", self.work / "gate.png",
                                "--ids", "gate-a", "gate-b", *self.render_options())
            assert rendered["data"]["pugc_sha256"] == measured["data"]["pugc_sha256"]
        return measured["data"]

    def tower(self):
        fixture = self.args.fixtures.resolve()
        baseline = fixture / "route.pugc"
        wrong = fixture / "wrong-tower.pugc"
        project, corrected = self.work / "project.json", self.work / "corrected.pugc"
        self.cli("import", wrong, project)
        inspected = self.cli("inspect", project, "--id", "tower")
        assert inspected["data"]["placements"][0]["id"] == "tower"
        receipt = json.loads(baseline.with_suffix(".pugc.json").read_bytes())
        index = next(i for i, item in enumerate(receipt["instances"]) if item["id"] == "tower")
        target = read_game(baseline)["objects"][index]["spawnTransform"]["translation"]
        self.edit(project, [{"op": "update", "id": "tower", "fields": {"at_cm": [target[axis] for axis in "xyz"]}}])
        self.cli("build", project, corrected)
        checked = self.cli("validate", corrected)
        before, after = read_game(wrong), read_game(corrected)
        assert [i for i, (a, b) in enumerate(zip(before["objects"], after["objects"], strict=True)) if a != b] == [index]
        assert after == read_game(baseline)
        if not self.args.no_render:
            for name, mapfile, code in (("route", baseline, 0), ("wrong", wrong, 2), ("corrected", corrected, 0)):
                response = self.cli("compare", fixture / "source.npz", mapfile, "--project", fixture / "route.json",
                                    "--strict", "--cameras", "Approach", "Alongside", "--out", self.work / name,
                                    *self.render_options(), code=code)
                if code:
                    assert [(row["camera"], row["issue"], row["instance_ids"]) for row in response["data"]["issues"]] == [
                        (name, "side_changed", ["tower"]) for name in ("Approach", "Alongside")]
        return {"pugc_sha256": checked["data"]["pugc_sha256"], "changed_id": "tower", "target_cm": target}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("gate", "tower"))
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--meshes", type=Path, required=True)
    parser.add_argument("--chromium", type=Path)
    parser.add_argument("--base-level", type=Path)
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--no-render", action="store_true")
    args = parser.parse_args()
    demo = Demo(args)
    data = (demo.gate if args.scenario == "gate" else demo.tower)()
    summary = {"scenario": args.scenario, "data": data, "calls": len(demo.trace),
               "builds": sum(row["arguments"][0] == "build" for row in demo.trace),
               "seconds": perf_counter() - demo.started,
               "stdout_bytes": sum(row["stdout_bytes"] for row in demo.trace)}
    write_json(demo.work / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
