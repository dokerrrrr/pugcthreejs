"""Reproduce the original Toolkit's route acceptance and capture its fixtures."""
import argparse
from hashlib import file_digest
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import venv

REVISION = "a660066f7133edaea76805338c05bec4d72cfd5b"
SOURCE = "https://github.com/sashakykyshka17-pixel/test-private-repository.git"
MAPS = ("route", "wrong-tower", "overpass")
SNAPSHOTS = (
    "inputs.json", "versions.json", "unittest.json", "source.npz", "route.json",
    "overpass.json", "before-bench-edit.pugc", "before-bench-edit.pugc.json",
    "broken-receipt.json", "partial-audit.json",
    *(f"{name}.pugc{suffix}" for name in MAPS for suffix in ("", ".json")),
)


def run(*args, **options):
    return subprocess.run(args, check=True, **options)


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def snapshot(python, work, output):
    output.mkdir(parents=True, exist_ok=True)
    for name in SNAPSHOTS:
        shutil.copyfile(work / name, output / name)
    for name in MAPS:
        program = ("from pugc_toolkit.pugc_codec import read_game; import json, sys; "
                   "print(json.dumps(read_game(sys.argv[1]), ensure_ascii=False))")
        decoded = run(python, "-IB", "-c", program, work / f"{name}.pugc", capture_output=True)
        write_json(output / f"{name}-game.json", json.loads(decoded.stdout))
    for name in ("route", "wrong-tower"):
        frames = output / f"{name}-frames"
        frames.mkdir(exist_ok=True)
        for file in (work / frames.name).iterdir():
            if file.suffix == ".png" or file.name == "comparison.json":
                shutil.copyfile(file, frames / file.name)
    statuses = {}
    for name in ("route", "wrong-tower", "partial-audit", "broken-pair", "write-failure"):
        data = json.loads((work / f"{name}.stdout").read_bytes())
        statuses[name] = {
            "exit_code": data["exit_code"],
            "diagnostics": [{key: row[key] for key in ("code", "instance_id", "object_id") if key in row}
                            for row in data["diagnostics"]],
        }
    write_json(output / "statuses.json", statuses)
    write_json(output / "sha256.json", {
        str(path.relative_to(output)): digest(path)
        for path in sorted(output.rglob("*")) if path.is_file() and path.name != "sha256.json"
    })


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, help="Existing Git checkout containing the pinned revision")
    parser.add_argument("--work", type=Path, required=True, help="Directory for original acceptance logs and inputs")
    parser.add_argument("--out", type=Path, required=True, help="Directory for the reference snapshot")
    args = parser.parse_args()
    if args.out.resolve() == args.work.resolve():
        parser.error("--out and --work must be separate")
    with TemporaryDirectory(prefix="pugc-baseline-") as scratch:
        root = Path(scratch)
        source = args.checkout.resolve() if args.checkout else root / "source"
        if args.checkout is None:
            run("git", "init", "-q", source)
            run("git", "-C", source, "remote", "add", "origin", SOURCE)
            run("git", "-C", source, "fetch", "--depth=1", "origin", REVISION)
        checkout = root / "checkout"
        build = root / "build"
        checkout.mkdir()
        build.mkdir()
        run("git", "clone", "--no-checkout", "--shared", source, checkout)
        env = {**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}
        run("git", "checkout", "--detach", REVISION, cwd=checkout, env=env)
        archive = run("git", "archive", REVISION, cwd=checkout, capture_output=True).stdout
        with tarfile.open(fileobj=io.BytesIO(archive)) as members:
            members.extractall(build, filter="data")
        virtual = root / "venv"
        venv.EnvBuilder(with_pip=True).create(virtual)
        python = virtual / "bin" / "python"
        env["PATH"] = f"{python.parent}{os.pathsep}{env['PATH']}"
        run(python, "-m", "pip", "install", f"{build}[geometry]")
        run(python, "-B", "tests/ci_acceptance.py", "integration", "--work", args.work.resolve(),
            cwd=checkout, env=env)
        snapshot(python, args.work.resolve(), args.out.resolve())


if __name__ == "__main__":
    main()
