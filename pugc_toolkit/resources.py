"""Filesystem locations shared by the wheel and the source checkout."""
import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
ROOT = PACKAGE_ROOT if (PACKAGE_ROOT / "catalog").is_dir() else PACKAGE_ROOT.parent
CATALOG = ROOT / "catalog"
ASSET_DIR = Path(os.getenv("PUGC_ASSET_DIR") or ROOT / "assets").expanduser().resolve()
MESHES = ASSET_DIR / "ugc_meshes_all.zip"
TEXTURES = ASSET_DIR / "PUGC_Textures_Ready.zip"
PROFILE = CATALOG / "placement_profile.json"
TEMPLATE = CATALOG / "pugc_v3_empty.json"
