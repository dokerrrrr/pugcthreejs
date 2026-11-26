"""Compatibility entry point for pugc_toolkit.mesh_library."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pugc_toolkit import mesh_library as _implementation


def __getattr__(name):
    return getattr(_implementation, name)
