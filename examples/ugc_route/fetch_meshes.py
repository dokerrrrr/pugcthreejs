"""Compatibility command for installing the small, pinned route pack."""
import argparse
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pugc_toolkit import asset_packs, cli_protocol as cli


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--archive", type=Path, help="Read from a local ZIP")
    args = parser.parse_args()
    try:
        result = asset_packs.fetch("small", args.output, archive=args.archive)
    except cli.INPUT_ERRORS as exc:
        parser.error(str(exc))
    if result["exit_code"]:
        parser.error(result["diagnostics"][0]["message"])
    pack = result["data"]["pack"]
    print(f"Verified {pack['meshes']} meshes in {pack['path']} ({args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
