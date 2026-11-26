"""Measure and place native meshes and terrain in game XYZ centimetres."""
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import cli_protocol as cli
from .base_level import WATER_Z_M, resolve_base_level
from .game_space import TRANSFORM, game_rotation_matrix
from .mesh_library import MeshLibrary
from .scene import file_sha256
from .ugc_project import raw_with_receipt, xyz

AXES = ("+X", "-X", "+Y", "-Y")


def oriented_vertices(mesh, transform):
    return (mesh.vertices @ game_rotation_matrix(transform).T) * 100


def world_vertices(mesh, transform):
    return oriented_vertices(mesh, transform) + xyz(transform["translation"])


@lru_cache(maxsize=4)
def _terrain_mesh(path, digest):
    try:
        import trimesh
    except ModuleNotFoundError as exc:
        raise cli.CliError(cli.RESOURCE, "trimesh_missing", "Terrain queries require trimesh.",
                           "Install the toolkit with the [geometry] extra.") from exc

    scene = trimesh.load(path, force="scene", process=False)
    parts = []
    for node in scene.graph.nodes_geometry:
        if not node.startswith("terrain_"):
            continue
        matrix, name = scene.graph[node]
        geom = scene.geometry[name]
        points = trimesh.transform_points(geom.vertices, matrix)[:, [0, 2, 1]] * 100
        tris = points[geom.faces]
        parts.append((tris, tris[:, :, :2].min(axis=1), tris[:, :, :2].max(axis=1)))
    if not parts:
        raise ValueError("The island has no terrain geometry")
    return parts


def terrain_path(local, *, offline=False):
    from argparse import Namespace
    return resolve_base_level(Namespace(base_level=local, offline=offline), cli.MachineParser())


def projected_heights(triangles, xy):
    start = triangles[:, 0]
    edge1, edge2 = triangles[:, 1] - start, triangles[:, 2] - start
    delta = xy - start[:, :2]
    determinant = edge1[:, 0] * edge2[:, 1] - edge1[:, 1] * edge2[:, 0]
    valid = abs(determinant) > 1e-9
    inv = np.divide(1., determinant, out=np.zeros_like(determinant), where=valid)
    u = (delta[:, 0] * edge2[:, 1] - delta[:, 1] * edge2[:, 0]) * inv
    v = (edge1[:, 0] * delta[:, 1] - edge1[:, 1] * delta[:, 0]) * inv
    valid = valid & (u >= -1e-9) & (v >= -1e-9) & (u + v <= 1 + 1e-9)
    return np.where(valid, start[:, 2] + u * edge1[:, 2] + v * edge2[:, 2], np.nan)


def terrain_height(path, x, y):
    if not np.isfinite([x, y]).all():
        raise ValueError("Expected finite game coordinates")
    point = np.asarray([x, y])
    levels = []
    for tris, lo, hi in _terrain_mesh(path, file_sha256(path)):
        if np.any(point < lo.min(0)) or np.any(point > hi.max(0)):
            continue
        selected = tris[np.all((lo <= point) & (hi >= point), axis=1)]
        z = projected_heights(selected, point)
        levels.extend(z[np.isfinite(z)])
    if not levels:
        raise cli.CliError(cli.RESOURCE, "terrain_uncovered", f"No terrain triangle at ({x}, {y}) cm.",
                           "Choose a location on the terrain or use water support.", path=path)
    return float(max(levels))


@dataclass(frozen=True)
class PlacementGeometry:
    meshes: Path
    base_level: Path | None

    @lru_cache(maxsize=64)
    def _mesh(self, oid, digest):
        with MeshLibrary(self.meshes) as library:
            return library.geometry(oid)

    def mesh(self, item):
        oid = item["objectId"]
        cli.archive_input(self.meshes, "--meshes", machine=True)
        try:
            geom = self._mesh(oid, file_sha256(self.meshes))
        except KeyError as exc:
            diagnostic = cli.missing_mesh(self.meshes, instance_id=item["id"], object_id=oid)
            raise cli.CliError(cli.RESOURCE, diagnostic["code"], diagnostic["message"], diagnostic["action"],
                               path=self.meshes, instance_id=item["id"], object_id=oid) from exc
        if not len(geom.triangles):
            raise cli.CliError(cli.RESOURCE, "mesh_empty", "Model has no triangles.",
                               "Choose another native object.", instance_id=item["id"], object_id=oid)
        return geom

    def height(self, support, x, y):
        if support == "water":
            return WATER_Z_M * 100
        return terrain_height(terrain_path(self.base_level), x, y)

    def contact(self, local, xy, support):
        low = local.min(0)
        # Use the model's lowest vertices. Terrain variation is reported across their footprint.
        feet = np.unique(np.round(local[local[:, 2] <= low[2] + .02, :2], 5), axis=0)
        if len(feet) > 24:
            corners = [feet[np.argmax(feet @ np.asarray(direction))]
                       for direction in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))]
            feet = np.unique(corners, axis=0)
        heights = [self.height(support, *coords) for coords in feet + xy]
        return {"surface": support, "height_cm": max(heights), "min_height_cm": min(heights),
                "samples": len(heights), "bottom_offset_cm": float(low[2])}

    def place_relative(self, entry, anchor, axis, desired, support):
        anchor_transform, placement = anchor[TRANSFORM], entry[TRANSFORM]
        anchor_vertices = world_vertices(self.mesh(anchor), anchor_transform)
        local = oriented_vertices(self.mesh(entry), placement)
        dimension, sign = axis_components(axis)
        at = np.asarray(xyz(anchor_transform["translation"]), dtype=float)
        at[dimension] = sign * ((sign * anchor_vertices[:, dimension]).max() + desired
                                - (sign * local[:, dimension]).min())
        if support is not None:
            contact = self.contact(local, at[:2], support)
            at[2] = contact["height_cm"] - contact["bottom_offset_cm"]
        placement["translation"].update(zip("xyz", at.tolist(), strict=True))
        measure = gap(anchor_vertices, local + at, axis)
        return measure, contact if support is not None else None

    def seat(self, entry, support):
        placement = entry[TRANSFORM]
        at = xyz(placement["translation"])
        local = oriented_vertices(self.mesh(entry), placement)
        contact = self.contact(local, at[:2], support)
        placement["translation"]["z"] = contact["height_cm"] - contact["bottom_offset_cm"]
        return contact


def axis_components(axis):
    if axis not in AXES:
        raise ValueError("Expected signed horizontal axis +X, -X, +Y or -Y")
    return "XY".index(axis[-1]), 1 if axis[0] == "+" else -1


def gap(first, second, axis):
    dimension, sign = axis_components(axis)
    a = float((sign * first[:, dimension]).max())
    b = float((sign * second[:, dimension]).min())
    cross = 1 - dimension
    overlap = float(min(first[:, cross].max(), second[:, cross].max())
                    - max(first[:, cross].min(), second[:, cross].min()))
    return {"axis": axis, "gap_cm": b - a, "anchor_face_cm": sign * a, "other_face_cm": sign * b,
            "cross_axis_overlap_cm": overlap, "method": "opposing support planes of native vertices"}


def written_placements(path, receipt_path=None):
    raw, receipt = raw_with_receipt(path, receipt_path)
    return {meta["id"]: {**meta, TRANSFORM: record[TRANSFORM]}
            for meta, record in zip(receipt["instances"], raw["objects"], strict=True)}, receipt["pugc_sha256"]


def verify_gap(path, first, second, axis, geometry, *, expected=None, tolerance=1, support=None, receipt=None):
    entries, digest = written_placements(path, receipt)
    if first == second or first not in entries or second not in entries:
        raise ValueError("Select two different placement IDs")
    for value in (expected, tolerance):
        if value is not None and (not np.isfinite(value) or value < 0):
            raise ValueError("Expected nonnegative finite widths and tolerances")
    a, b = entries[first], entries[second]
    result = gap(world_vertices(geometry.mesh(a), a[TRANSFORM]), world_vertices(geometry.mesh(b), b[TRANSFORM]), axis)
    result.update(pugc_sha256=digest, ids=[first, second], tolerance_cm=tolerance, coverage="complete")
    result["fits"] = result["cross_axis_overlap_cm"] > 0 and (
        expected is None or abs(result["gap_cm"] - expected) <= tolerance)
    if support is not None:
        checks = []
        for item in (a, b):
            t = item[TRANSFORM]
            at = xyz(t["translation"])
            details = geometry.contact(oriented_vertices(geometry.mesh(item), t), at[:2], support)
            details["clearance_cm"] = at[2] + details["bottom_offset_cm"] - details["height_cm"]
            checks.append({"id": item["id"], **details})
        result["contacts"] = checks
        result["fits"] &= all(abs(row["clearance_cm"]) <= tolerance for row in checks)
    return result
