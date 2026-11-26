"""Source and coordinates for the environment shared by PUGC visualizations."""
from hashlib import sha256
import os
from pathlib import Path

from . import cli_protocol as cli
from .resources import ASSET_DIR

# Origin links recorded in reports for provenance.
SCENE_EDITOR_URL = "https://plzdntkilmeh.github.io/pugc-editor/scene-editor.html"
ISLAND_REVISION = "3bd69a10c3ad9ebe8f9aa44964a36784b7bce29b"
ISLAND_URL = (
    f"https://raw.githubusercontent.com/PlzDntKilMeh/pugc-editor/{ISLAND_REVISION}/cache/mod_main_level.glb"
)
ISLAND_SHA256 = "e72dbad55c8c1499ad899b9e50707a0c657952c60024b94a21043fd15d7540dd"
ISLAND_SIZE = 11_579_416
ISLAND_BOUNDS_M = (0, 0, 1020, 1020)
ISLAND_Z_RANGE_M = (-204.19, 17.31)
WATER_BOUNDS_M = (-5000, -5000, 6020, 6020)
WATER_Z_M = 0.0  # The scene editor's world grid is at Three.js Y = 0, which maps to game Z = 0.
TERRAIN_COLOR = (.54, .47, .34)
ROCK_COLOR = (.46, .44, .4)
WATER_COLOR = (.12, .36, .53)


def add_arguments(parser):
    parser.add_argument("--base-level", type=Path, help="Local copy of the verified island GLB")


def _verified(data):
    return len(data) == ISLAND_SIZE and sha256(data).hexdigest() == ISLAND_SHA256


def resolve_base_level(args, parser):
    if args.base_level is not None:
        candidates = (args.base_level,)
    else:
        cache = Path(os.getenv("XDG_CACHE_HOME") or Path.home() / ".cache") / "pugc-toolkit"
        candidates = (ASSET_DIR / "mod_main_level.glb", cache / f"mod_main_level-{ISLAND_REVISION[:8]}.glb")
    path = candidates[0].expanduser().resolve()
    try:
        for candidate in candidates:
            resolved = candidate.expanduser().resolve()
            if resolved.is_file() and _verified(resolved.read_bytes()):
                return resolved
        raise ValueError("File is missing or its SHA-256 differs from the pinned island GLB")
    except (OSError, ValueError) as exc:
        message = f"Cannot load the starting island ({path}): {exc}"
        action = "Pass a verified GLB with --base-level or place mod_main_level.glb in PUGC_ASSET_DIR."
        if isinstance(parser, cli.MachineParser):
            raise cli.CliError(cli.RESOURCE, "base_level_unavailable", message, action,
                               path=path, field="--base-level") from exc
        parser.error(f"{message}. {action}")


def environment_metadata():
    return {
        "scene_editor_url": SCENE_EDITOR_URL,
        "island_url": ISLAND_URL,
        "island_sha256": ISLAND_SHA256,
        "island_bounds_m": ISLAND_BOUNDS_M,
        "island_z_range_m": ISLAND_Z_RANGE_M,
        "water_bounds_m": WATER_BOUNDS_M,
        "water_z_m": WATER_Z_M,
        "water_z_source": "scene_editor_grid",
    }
