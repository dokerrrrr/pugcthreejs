"""Read transforms stored by Toolkit versions with per-mesh frames."""
from copy import deepcopy
from functools import lru_cache
import json

from .resources import CATALOG


@lru_cache(maxsize=1)
def _model_frames():
    path = CATALOG / "legacy_layout_centres.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _model_frame(game_entry):
    model = _model_frames().get(str(game_entry["objectId"]))
    return model if model is not None and game_entry.get("deviceIndex", -1) == -1 else ((0., 0., 0.), 0)


def _rotation_components(rotation, axis):
    x, y, z, w = (rotation[key] for key in "xyzw")
    return ((x, -y, -z, w), (-y, -x, w, z), (z, w, x, y))[axis]


def legacy_transform(game_entry, origin):
    """Reconstruct a historical authoring snapshot for compatibility checks."""
    center, axis = _model_frame(game_entry)
    transform = deepcopy(game_entry["spawnTransform"])
    q, p, scale = transform["rotation"], transform["translation"], transform["scale3D"]
    x, y, z, w = (q[key] for key in "xyzw")
    norm = x*x + y*y + z*z + w*w
    columns = ((1-2*(y*y+z*z)/norm, 2*(x*y+w*z)/norm, 2*(x*z-w*y)/norm),
               (2*(x*y-w*z)/norm, 1-2*(x*x+z*z)/norm, 2*(y*z+w*x)/norm),
               (2*(x*z+w*y)/norm, 2*(y*z-w*x)/norm, 1-2*(x*x+y*y)/norm))
    delta = 200.0 * center[axis] * scale["xyz"[axis]]
    dx, dy, dz = (delta * value for value in columns[axis])
    p.update(x=round(2*origin-p["x"]-dx, 8), y=round(p["y"]+dy, 8), z=round(p["z"]+dz, 8))
    q.update(zip("xyzw", _rotation_components(q, axis)))
    return transform


def rebase_legacy_transform(placement, original, current, origin):
    """Move edits from the historical frame to the current native pivot."""
    old = legacy_transform(original, origin)
    translation = placement["translation"]
    position = {
        axis: current["translation"][axis] + round(translation[axis] - old["translation"][axis], 8)
        for axis in "xyz"
    }
    _, axis = _model_frame(original)
    x, y, z, w = _rotation_components(placement["rotation"], axis)
    return {**placement, "translation": {**translation, **position},
            "rotation": {**placement["rotation"], "x": x, "y": -y, "z": -z, "w": w}}
