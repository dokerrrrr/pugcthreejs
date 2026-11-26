"""Game-space transforms and the single conversion to the pugc-editor view basis."""
from copy import deepcopy
from math import cos, radians, sin

PROJECT_SCHEMA = 2
SPACE = "game-xyz-cm"
TRANSFORM = "spawnTransform"


def is_native(project):
    return project.get("schema") == PROJECT_SCHEMA


def transform_key(project):
    return TRANSFORM if is_native(project) else "transform"


def transform_cm(position_cm, yaw_deg=0, scale=(1, 1, 1)):
    angle = radians(yaw_deg) / 2
    return {"translation": dict(zip("xyz", position_cm, strict=True)),
            "rotation": dict(x=0, y=0, z=sin(angle), w=cos(angle)),
            "scale3D": dict(zip("xyz", scale, strict=True))}


def game_to_view(position_cm):
    x, y, z = position_cm
    return [x / 100, z / 100, y / 100]


def view_to_game(position_m):
    x, y, z = position_m
    return [x * 100, z * 100, y * 100]


def view_components(placement):
    t, q, s = (placement[k] for k in ("translation", "rotation", "scale3D"))
    return game_to_view([t[k] for k in "xyz"]), [-q["x"], -q["z"], -q["y"], q["w"]], [s[k] for k in "xzy"]


def native_records(project):
    records = []
    for item in project["instances"]:
        record = deepcopy(item.get("game_original", {"deviceIndex": -1, "userDeviceName": ""}))
        record.update(objectId=item["objectId"], spawnTransform=item[TRANSFORM])
        records.append(record)
    return {**project["game"], "objects": records}


def game_rotation_matrix(placement):
    """Rotate a game XYZ column vector by a possibly unnormalized XYZW quaternion."""
    from .ugc_geometry import placement_matrix

    return placement_matrix(placement)[:3, :3]


def view_placement_matrix(placement):
    from .ugc_geometry import placement_matrix

    position, rotation, scale = view_components(placement)
    return placement_matrix({"translation": dict(zip("xyz", (value * 100 for value in position))),
                             "rotation": dict(zip("xyzw", rotation)), "scale3D": dict(zip("xyz", scale))})


def view_to_game_quaternion(rotation):
    x, y, z, w = rotation
    return [-x, -z, -y, w]
