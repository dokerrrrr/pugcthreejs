"""Read complete instanced UGC meshes in XYZ metres."""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import gzip
import json
import struct
import zipfile

import numpy as np


@dataclass(frozen=True)
class Section:
    first_triangle: int
    triangle_count: int
    material: str


@dataclass(frozen=True)
class Geometry:
    vertices: np.ndarray
    triangles: np.ndarray
    triangle_uvs: np.ndarray
    sections: tuple[Section, ...]
    part_count: int
    instance_count: int
    normals: np.ndarray | None = None


def parse_geometry(raw, object_id, *, authoring=False, view=False):
    offset = 0

    def take(dtype, count):
        nonlocal offset
        values = np.frombuffer(raw, dtype=dtype, count=count, offset=offset)
        offset += values.nbytes
        return values

    def u32():
        return int(take("<u4", 1)[0])

    if u32() != 0xFFFFFFFE:
        raise ValueError(f"UGC {object_id}: invalid mesh magic")
    part_count = u32()
    if not 0 < part_count <= 1000:
        raise ValueError(f"UGC {object_id}: invalid mesh part count {part_count}")
    parts = []
    for _ in range(part_count):
        count = u32()
        if count % 3 or count > 3_000_000:
            raise ValueError(f"UGC {object_id}: invalid vertex count {count}")
        vertices = take("<f4", 3 * count).reshape(-1, 3)
        normals = take("<f4", 3 * count).reshape(-1, 3)
        uv = take("<f4", 2 * count).reshape(-1, 3, 2)
        sections = []
        for _ in range(u32()):
            first_triangle, triangle_count, length = struct.unpack_from("<III", raw, offset)
            offset += 12
            material = take("u1", length).tobytes().decode("utf8", "replace")
            if first_triangle + triangle_count > count // 3:
                raise ValueError(f"UGC {object_id}: material section outside its mesh")
            sections.append(Section(first_triangle, triangle_count, material))
        parts.append((vertices, normals, uv, sections))
    instance_count = u32()
    if instance_count > 20000:
        raise ValueError(f"UGC {object_id}: invalid instance count {instance_count}")
    world_vertices, triangle_uvs, world_sections, reverse = [], [], [], []
    world_normals = []
    first = 0
    for _ in range(instance_count):
        part_index = u32()
        if part_index >= part_count:
            raise ValueError(f"UGC {object_id}: invalid instance part index {part_index}")
        vertices, normals, uv, sections = parts[part_index]
        matrix = take("<f4", 16).reshape(4, 4).T
        world = vertices @ matrix[:3, :3].T + matrix[:3, 3]
        count = len(vertices) // 3
        # Native buffers swap Y and Z. Authoring meshes also reflect local X.
        determinant = np.linalg.det(matrix[:3, :3])
        flip = determinant < 0 if view else (determinant > 0) != authoring
        points = world if view else world[:, [0, 2, 1]]
        if view:
            normal = normals @ np.linalg.pinv(matrix[:3, :3])
            length = np.linalg.norm(normal, axis=1, keepdims=True)
            np.divide(normal, length, out=normal, where=length > 0)
            world_normals.append(normal)
        if authoring:
            points[:, 0] *= -1
        world_vertices.append(points)
        triangle_uvs.append(uv[:, [0, 2, 1]] if flip else uv)
        reverse.extend([flip] * count)
        world_sections.extend(Section(first + entry.first_triangle, entry.triangle_count, entry.material)
                              for entry in sections)
        first += count
    if offset != len(raw):
        raise ValueError(f"UGC {object_id}: unexpected bytes after mesh instances")
    points = np.concatenate(world_vertices) if world_vertices else np.empty((0, 3), dtype=np.float32)
    uv = np.concatenate(triangle_uvs) if triangle_uvs else np.empty((0, 3, 2), dtype=np.float32)
    triangles = np.arange(len(points), dtype=np.int32).reshape(-1, 3)
    reverse = np.asarray(reverse, dtype=bool)
    triangles[reverse] = triangles[reverse][:, [0, 2, 1]]
    normals = np.concatenate(world_normals) if world_normals else None
    return Geometry(points, triangles, uv, tuple(world_sections), part_count, instance_count, normals)


class MeshLibrary:
    def __init__(self, zip_path, *, authoring=False, view=False):
        self.zip_path = str(zip_path)
        self._authoring = authoring
        self._view = view
        self._z = zipfile.ZipFile(self.zip_path)
        self.ids = {int(path.rsplit("/", 1)[-1].split(".")[0]) for path in self._z.namelist()
                    if path.startswith("bp-mesh/") and path.endswith(".bin.gz")}
        self._geometry = lru_cache(maxsize=16)(self._read_geometry)

    def _read_geometry(self, object_id):
        if object_id not in self.ids:
            raise KeyError(f"UGC mesh {object_id} missing from {self.zip_path}")
        geometry = parse_geometry(gzip.decompress(self._z.read(f"bp-mesh/{object_id}.bin.gz")), object_id,
                                  authoring=self._authoring, view=self._view)
        for array in (geometry.vertices, geometry.triangles, geometry.triangle_uvs):
            array.setflags(write=False)
        return geometry

    def geometry(self, object_id):
        return self._geometry(int(object_id))

    def metadata(self, object_id):
        return json.loads(self._z.read(f"bp-mesh/{int(object_id)}.json"))

    def load(self, object_id, cap_vertices=100000):
        """Compatibility API returning complete bounds and optionally sampled points."""
        if cap_vertices is not None and cap_vertices <= 0:
            raise ValueError("cap_vertices must be positive or None")
        object_id = int(object_id)
        geometry = self.geometry(object_id)
        points = geometry.vertices
        minimum = points.min(axis=0) if len(points) else np.zeros(3)
        maximum = points.max(axis=0) if len(points) else np.zeros(3)
        if cap_vertices is not None and len(points) > cap_vertices:
            points = points[np.linspace(0, len(points) - 1, cap_vertices, dtype=np.intp)]
        return {
            "id": object_id, "bounds_min": minimum.tolist(), "bounds_max": maximum.tolist(),
            "dimensions": (maximum - minimum).tolist(), "center": ((maximum + minimum) / 2).tolist(),
            "mesh_vertices": len(geometry.vertices), "mesh_parts": geometry.part_count,
            "mesh_instances": geometry.instance_count,
            "materials": list(dict.fromkeys(section.material for section in geometry.sections)),
            "sampled_vertices": len(points),
        }, points

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._geometry.cache_clear()
        self._z.close()
