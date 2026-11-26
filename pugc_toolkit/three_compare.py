"""Camera-matched numerical observations and Three.js comparison frames."""
from functools import lru_cache
from hashlib import sha256

import numpy as np

from . import cli_protocol as cli
from . import geometry_report as coverage
from .scene import instance_bounds
from .triangle_index import TriangleIndex, intersects
from .ugc_geometry import apply_matrix
from .ugc_storage import json_bytes

ALGORITHM = 1


@lru_cache(maxsize=64)
def _tree(digest, vertices, triangles):
    return TriangleIndex(np.frombuffer(vertices, dtype="<f4").reshape(-1, 3),
                         np.frombuffer(triangles, dtype="<u4").reshape(-1, 3))


class Observations:
    def __init__(self, scene):
        self.scene = scene
        self.instances, self.references = [], {}
        for item in scene.manifest["instances"]:
            if not item.get("model"):
                continue
            resource = scene.models[item["model"]]
            matrix = np.asarray(item["matrix"])
            inverse = np.linalg.inv(matrix)
            bounds = instance_bounds(scene.manifest, [item])
            index = len(self.instances)
            self.instances.append((resource, matrix, inverse, np.asarray(bounds)))
            for name in item.get("references", [item["id"]]):
                self.references.setdefault(name, []).append(index)

    def cast(self, origin, target):
        distance = np.linalg.norm(target - origin)
        direction = (target - origin) / distance
        best, limit = None, distance + .01
        for index, (resource, _, inverse, bounds) in enumerate(self.instances):
            if not intersects(origin, direction, bounds[0] - 1e-7, bounds[1] + 1e-7, limit):
                continue
            local = apply_matrix(origin, inverse)
            ray = inverse[:3, :3] @ direction
            ratio = np.linalg.norm(ray)
            tree = _tree(resource.asset["sha256"], resource.vertices.tobytes(), resource.triangles.tobytes())
            hit = tree.ray_cast(local, ray, limit * ratio)[3]
            if hit is not None and (best is None or hit / ratio < limit):
                best, limit = index, hit / ratio
        return best

    def observe(self, camera, refs):
        group = {index for key in refs for index in self.references[key]}
        world = np.asarray(camera["matrix"])
        view = np.linalg.inv(world)
        near, far = camera["planes"][-2:]

        def project(points):
            local = apply_matrix(points, view)
            depth = -local[:, 2]
            left, right, bottom, top = camera["planes"][:4]
            ratio = near / np.where(abs(depth) < 1e-9, 1e-9, depth) if camera["type"] == "PERSP" else 1
            return np.column_stack(((local[:, 0] * ratio - left) / (right - left),
                                    (local[:, 1] * ratio - bottom) / (top - bottom), depth))

        members = [self.instances[index] for index in sorted(group)]
        vertices = [apply_matrix(mesh.vertices, matrix) for mesh, matrix, _, _ in members]
        all_points = np.concatenate(vertices)
        center = (all_points.min(0) + all_points.max(0)) / 2
        x, y, _ = project(center[None])[0]
        visible = False
        for points, (mesh, _, _, _) in zip(vertices, members):
            indices = np.linspace(0, len(mesh.triangles) - 1, min(28, len(mesh.triangles)), dtype=int)
            samples = points[mesh.triangles[indices]].mean(axis=1)
            coords = project(samples)
            for sample, (u, v, depth) in zip(samples, coords):
                if not (0 <= u <= 1 and 0 <= v <= 1 and near <= depth <= far):
                    continue
                origin = world[:3, 3] if camera["type"] == "PERSP" else sample + world[:3, 2] * depth
                if self.cast(origin, sample) in group:
                    visible = True
                    break
            if visible:
                break
        return {"side": "left" if x < .495 else "right" if x > .505 else "center",
                "visible": visible, "projection": [float(x), float(y)],
                "center_cm": (center[[0, 2, 1]] * 100).tolist()}


def compare(source, result, project, cameras=None, strict=False):
    by_name = {camera["id"]: camera for camera in source.manifest["cameras"]}
    by_name.update({camera["name"]: camera for camera in source.manifest["cameras"]
                    if not camera["source_record"]["instance"]})
    keys = cameras or [source.manifest["active_camera"] or next(iter(by_name), "active source camera")]
    missing_cameras = [name for name in keys if name not in by_name]
    source_geo, result_geo = Observations(source), Observations(result)
    expected = checked = 0
    missing_landmarks, frames, issues = [], [], []
    for key in keys:
        camera = by_name.get(key)
        labels = {key, camera["id"], camera["name"]} if camera else {key}
        landmarks = [row for row in project.get("landmarks", []) if not row.get("cameras")
                     or labels.intersection(row["cameras"])]
        expected += len(landmarks)
        if not camera:
            missing_landmarks.extend({"landmark": row["name"], "camera": key, "reason": "camera_unavailable"}
                                     for row in landmarks)
            continue
        entry = {"camera": camera["name"], "camera_id": camera["id"], "landmarks": [],
                 "resolution": camera["resolution"], "projection": camera["type"], "planes": camera["planes"],
                 "matrix_world": camera["matrix"], "source": f"{len(frames) + 1:02}-source.png",
                 "result": f"{len(frames) + 1:02}-result.png"}
        frames.append(entry)
        for landmark in landmarks:
            initial, final = landmark.get("source", []), landmark.get("result", [])
            missing_source = [name for name in initial if name not in source_geo.references]
            missing_result = [name for name in final if name not in result_geo.references]
            row = {"landmark": landmark["name"], "camera": camera["name"]}
            if not initial or not final or missing_source or missing_result:
                missing_landmarks.append({**row, "reason": "geometry_unavailable",
                                          "missing_source_ids": missing_source, "missing_result_ids": missing_result})
                continue
            checked += 1
            a, b = source_geo.observe(camera, initial), result_geo.observe(camera, final)
            row.update(source=a, result=b, instance_ids=final)
            entry["landmarks"].append(row)
            if a["visible"] and (a["side"] != b["side"] or not b["visible"]):
                issues.append({**row, "issue": "side_changed" if a["side"] != b["side"] else "lost_visibility"})
    total = len(result.manifest["instances"])
    missing = result.manifest["missing_meshes"]
    checks = coverage.summary(strict,
        instances=coverage.check(total, total - len(missing), [row["instance"] for row in missing]),
        cameras=coverage.check(len(keys), len(frames), missing_cameras),
        landmarks=coverage.check(expected, checked, missing_landmarks, findings=len(issues), required=strict,
                                 requested_item="project.landmarks for selected cameras"))
    identity = {"algorithm": ALGORITHM, "map": result.manifest["pugc_sha256"],
                "placements": result.manifest["instances"],
                "mesh": result.manifest["mesh_sha256"], "reference": source.manifest["source_sha256"],
                "cameras": keys, "project": project, "mode": result.manifest.get("mode", "geometry"), "strict": strict,
                "environment": result.manifest["environment"], "materials": result.manifest["models"]}
    report = {"pugc_sha256": result.manifest["pugc_sha256"], "comparison_key": sha256(json_bytes(identity)).hexdigest(),
              "geometry_check": checks, "frames": frames, "issues": issues, "missing_meshes": missing}
    return report, by_name
