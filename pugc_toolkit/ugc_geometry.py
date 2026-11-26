"""Geometry shared by PUGC render and route audits."""
import json

import numpy as np

from .ugc_project import xyz


def placement_matrix(transform):
    x, y, z, w = (transform["rotation"][key] for key in "xyzw")
    norm = x*x + y*y + z*z + w*w
    if norm == 0:
        raise ValueError("Zero placement quaternion")
    matrix = np.eye(4)
    matrix[:3, :3] = np.array([
        [1-2*(y*y+z*z)/norm, 2*(x*y-w*z)/norm, 2*(x*z+w*y)/norm],
        [2*(x*y+w*z)/norm, 1-2*(x*x+z*z)/norm, 2*(y*z-w*x)/norm],
        [2*(x*z-w*y)/norm, 2*(y*z+w*x)/norm, 1-2*(x*x+y*y)/norm],
    ]) * xyz(transform["scale3D"])
    matrix[:3, 3] = np.asarray(xyz(transform["translation"])) / 100
    return matrix


def apply_matrix(points, matrix):
    return points @ matrix[:3, :3].T + matrix[:3, 3]


def source_geometry(path, scene_name=None):
    with np.load(path, allow_pickle=False) as archive:
        metadata = json.loads(archive["metadata"].item())
        array = {key: archive[key] for key in ("verts", "triangles", "triangle_uvs", "materials")}
    scenes = {entry["name"]: entry for entry in metadata["scenes"]}
    if scene_name is None:
        if len(scenes) != 1:
            raise ValueError("Select one Blender scene with --scene")
        scene_name = next(iter(scenes))
    scene = scenes[scene_name]
    return (scene, [entry for entry in metadata["objects"] if entry["scene"] == scene_name],
            [entry for entry in metadata["cameras"] if entry["scene"] == scene_name],
            metadata["material_slots"], array)
