"""Intersect finite segments in metres with the native triangles of PUGC placements."""
from typing import NamedTuple

import numpy as np

from .ugc_geometry import placement_matrix
from .triangle_index import TriangleIndex


class RayHit(NamedTuple):
    index: int  # Index in the sequence of placements passed to MeshRayScene.
    object_id: int
    point: np.ndarray


class MeshRayScene:
    """Bounds per placement, one local-space BVH per model hit by a query.

    origin_m is subtracted from placement translations to match the reference scene.
    geometry_for_id must remain available while casting, including for ZIP-backed meshes.
    """

    def __init__(self, records, geometry_for_id, origin_m):
        self._geometry = geometry_for_id
        self._trees = {}
        self._instances = []
        corners = {}
        lower, upper = [], []
        for record in records:
            oid = record["objectId"]
            if oid not in corners:
                vertices = geometry_for_id(oid).vertices
                low, high = vertices.min(0), vertices.max(0)
                corners[oid] = np.array([(x, y, z) for x in (low[0], high[0])
                                         for y in (low[1], high[1]) for z in (low[2], high[2])])
            mat = placement_matrix(record["spawnTransform"])
            mat[:3, 3] -= origin_m
            box = corners[oid] @ mat[:3, :3].T + mat[:3, 3]
            lower.append(box.min(0))
            upper.append(box.max(0))
            self._instances.append((oid, mat, np.linalg.inv(mat)))
        self._lower = np.asarray(lower).reshape(-1, 3)
        self._upper = np.asarray(upper).reshape(-1, 3)

    def cast(self, start, end, *, exclude_ids=()):
        """Return the nearest hit, or None. Ties retain the first placement."""
        start, end = np.asarray(start), np.asarray(end)
        delta = end - start
        low, high = np.minimum(start, end), np.maximum(start, end)
        overlaps = np.all((self._lower <= high + 1e-4) & (self._upper >= low - 1e-4), axis=1)
        nearest = np.inf
        best = None
        for index in np.flatnonzero(overlaps):
            oid, mat, inv = self._instances[index]
            if oid in exclude_ids:
                continue
            local = inv[:3, :3] @ start + inv[:3, 3]
            direction = inv[:3, :3] @ delta
            length = np.linalg.norm(direction)
            if length < 1e-7:
                continue
            if oid not in self._trees:
                mesh = self._geometry(oid)
                self._trees[oid] = TriangleIndex(mesh.vertices, mesh.triangles)
            hit = self._trees[oid].ray_cast(local, direction / length, length)[0]
            if hit is None:
                continue
            world = mat[:3, :3] @ np.asarray(hit) + mat[:3, 3]
            distance = np.linalg.norm(world - start)
            if distance < nearest:
                best, nearest = RayHit(int(index), oid, world), distance
        return best
