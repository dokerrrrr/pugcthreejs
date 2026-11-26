"""Versioned scene data for geometry checks and headless rendering."""
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from hashlib import file_digest, sha256
from itertools import product
import math
from pathlib import Path

import numpy as np

from .base_level import ISLAND_SHA256, environment_metadata
from .game_space import SPACE, TRANSFORM, game_to_view, is_native, view_placement_matrix
from .inspect_project import project_budget, read_project
from .mesh_library import MeshLibrary
from .resources import PROFILE
from .ugc_geometry import apply_matrix
from .ugc_project import DEFAULT_ORIGIN_X_CM, _game_records, load_json, raw_with_receipt

SCENE_SCHEMA = 1
IDENTITY = np.eye(4)


@lru_cache(maxsize=32)
def _file_digest(path, signature):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def file_sha256(path):
    path = Path(path).resolve()
    stat = path.stat()
    return _file_digest(path, (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))


def legacy_authoring_to_view(origin):
    return np.array([[-1, 0, 0, 2 * origin / 100], [0, 0, 1, 0],
                     [0, 1, 0, 0], [0, 0, 0, 1]])


def corners(low, high):
    return np.array(list(product(*zip(low, high))))


@dataclass
class Model:
    key: str
    asset: dict
    data: bytes
    vertices: np.ndarray
    triangles: np.ndarray


def model(vertices, triangles, uv, sections=(), normals=None):
    """Pack little-endian XYZ, the triangle indices and UVs in triangle corner order."""
    points = np.ascontiguousarray(vertices, dtype="<f4")
    faces = np.ascontiguousarray(triangles, dtype="<u4")
    texcoords = np.ascontiguousarray(uv, dtype="<f4")
    if not len(faces) or points.ndim != 2 or points.shape[1] != 3 or faces.shape != texcoords.shape[:2]:
        raise ValueError("Expected a renderable triangle mesh with per-corner UVs")
    if int(faces.max()) >= len(points) or not np.isfinite(points).all() or not np.isfinite(texcoords).all():
        raise ValueError("Invalid mesh coordinates or triangle index")
    if normals is not None:
        normals = np.ascontiguousarray(normals, dtype="<f4")
        if normals.shape != points.shape or not np.isfinite(normals).all():
            raise ValueError("Invalid mesh normals")
    data = points.tobytes() + faces.tobytes() + texcoords.tobytes()
    if normals is not None:
        data += normals.tobytes()
    digest = sha256(data).hexdigest()
    # Section layout affects materials even when geometry is shared.
    sections = list(sections)
    key = digest if not sections else sha256(bytes.fromhex(digest) + repr(sections).encode()).hexdigest()
    minimum, maximum = points.min(0).tolist(), points.max(0).tolist()
    asset = {"url": f"/blobs/{digest}.bin", "sha256": digest, "vertices": len(points),
             "triangles": len(faces), "bounds": [minimum, maximum], "sections": sections,
             "normals": normals is not None}
    return Model(key, asset, data, points, faces)


@lru_cache(maxsize=64)
def _native_model(path, archive_sha256, object_id):
    with MeshLibrary(path, view=True) as lib:
        geo = lib.geometry(object_id)
        return model(geo.vertices, geo.triangles, geo.triangle_uvs, map(asdict, geo.sections), geo.normals)


@dataclass
class Scene:
    manifest: dict
    blobs: dict = field(default_factory=dict)
    models: dict = field(default_factory=dict)

    def add_model(self, resource):
        self.models[resource.key] = resource
        self.manifest["models"][resource.key] = dict(resource.asset)
        self.blobs[resource.asset["url"]] = resource.data

    def add(self, resource, matrix, **fields):
        self.add_model(resource)
        self.manifest["instances"].append({**fields, "model": resource.key, "matrix": matrix.tolist()})

    def bounds(self):
        return instance_bounds(self.manifest, self.manifest["instances"])


def instance_bounds(manifest, instances):
    boxes = []
    for instance in instances:
        geometry = manifest["models"].get(instance.get("model"))
        if geometry:
            boxes.append(apply_matrix(corners(*geometry["bounds"]), np.asarray(instance["matrix"])))
    return [np.min([p.min(0) for p in boxes], axis=0).tolist(),
            np.max([p.max(0) for p in boxes], axis=0).tolist()] if boxes else None


def new_scene(kind, name, *, pugc_sha256=None, project_sha256=None, origin=50000, island=None):
    env = None
    if island is not None:
        env = {**environment_metadata(), "url": f"/blobs/{ISLAND_SHA256}.glb",
               "island_position": [0, 0, 0], "island_scale": [1, 1, 1]}
    result = Scene({"schema": SCENE_SCHEMA, "kind": kind, "name": name, "space": "three-y-up-metres",
                    "annotation_origin_x_cm": origin, "coordinate_space": SPACE, "pugc_sha256": pugc_sha256, "project_sha256": project_sha256,
                    "models": {}, "instances": [], "cameras": [], "missing_meshes": [], "environment": env})
    if env is not None:
        result.blobs[env["url"]] = Path(island)
    return result


def map_scene(path, meshes, *, receipt_path=None, origin_x_cm=None, profile=PROFILE, island=None):
    path = Path(path)
    project = None
    if path.suffix.lower() == ".pugc":
        raw, receipt = raw_with_receipt(path, receipt_path, origin_x_cm)
        entries = [{"assembly": "Imported", "zone": "near", **entry} for entry in receipt["instances"]]
        revision = None
        map_hash = receipt["pugc_sha256"]
        origin = receipt.get("origin_x_cm", DEFAULT_ORIGIN_X_CM)
    else:
        project, revision = read_project(path)
        raw = _game_records(project)
        entries = project["instances"]
        origin = project.get("origin_x_cm", DEFAULT_ORIGIN_X_CM)
        map_hash = None
    scene = new_scene("build" if project is None else "draft", raw.get("name"), pugc_sha256=map_hash,
                      project_sha256=revision, origin=origin, island=island)
    if project is not None:
        scene.manifest["editable"] = is_native(project)
        scene.manifest["budget"] = project_budget(project, profile)
        scene.manifest["approved_road"] = list(project.get("approved_road", {}))
    else:
        from .ugc_validation import budget_rows

        scene.manifest["budget"] = budget_rows(raw["objects"], load_json(profile), [e["zone"] for e in entries])
    archive = Path(meshes).resolve()
    archive_hash = file_sha256(archive)
    with MeshLibrary(archive, view=True) as library:
        ids = library.ids
    scene.manifest["mesh_sha256"] = archive_hash
    for record, entry in zip(raw["objects"], entries, strict=True):
        oid = record["objectId"]
        fields = {**{k: entry[k] for k in ("id", "assembly", "zone")}, "objectId": oid}
        transform = record[TRANSFORM]
        fields[TRANSFORM] = transform
        fields["transform"] = {"at_cm": [transform["translation"][axis] for axis in "xyz"],
                               "rotation_xyzw": [transform["rotation"][axis] for axis in "xyzw"],
                               "scale": [transform["scale3D"][axis] for axis in "xyz"]}
        if oid not in ids:
            scene.manifest["missing_meshes"].append({"instance": fields["id"], "objectId": oid})
            scene.manifest["instances"].append({**fields, "model": None})
            continue
        resource = _native_model(archive, archive_hash, oid)
        scene.add(resource, view_placement_matrix(transform), **fields)
    scene.manifest["bounds"] = scene.bounds()
    return scene


def alignment_matrix(project, units):
    matrix = np.array(project.get("source_to_authoring_m", IDENTITY), dtype=float)
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all() or not np.allclose(matrix[3], [0, 0, 0, 1]):
        raise ValueError("source_to_authoring_m must be a finite affine 4×4 matrix")
    matrix = matrix @ np.diag([units, units, units, 1])
    linear = matrix[:3, :3]
    ratio = np.linalg.norm(linear[:, 0])
    if ratio <= 0 or np.linalg.det(linear) <= 0 or not np.allclose(linear.T @ linear / ratio**2, np.eye(3)):
        raise ValueError("Source alignment requires positive uniform scale and rotation")
    return matrix, ratio


def source_camera(record, alignment, ratio, origin):
    width, height = record["resolution"]
    x_aspect, y_aspect = record["pixel_aspect"]
    y_correction = y_aspect / x_aspect
    ortho = record["type"] == "ORTHO"
    near, far = record["clip_start"] * ratio, record["clip_end"] * ratio
    sensor_fit = record["sensor_fit"]
    vertical = sensor_fit == "VERTICAL" or sensor_fit == "AUTO" and width < y_correction * height
    dimension = y_correction * height if vertical else width
    sensor = record["sensor_height"] if sensor_fit == "VERTICAL" else record["sensor_width"]
    unit = (record["ortho_scale"] * ratio if ortho else near * sensor / record["lens_mm"]) / dimension
    shift_x, shift_y = (record[f"shift_{axis}"] * dimension for axis in "xy")
    left, right = [(shift_x + side * width / 2) * unit for side in (-1, 1)]
    bottom, top = [(shift_y + side * height * y_correction / 2) * unit for side in (-1, 1)]
    world = legacy_authoring_to_view(origin) @ alignment @ np.asarray(record["matrix_world"])
    world[:3, :3] /= np.linalg.norm(world[:3, :3], axis=0)
    return {"id": record["id"], "name": record["name"], "type": record["type"], "matrix": world.tolist(),
            "resolution": [width, height], "planes": [left, right, bottom, top, near, far],
            "source_record": record}


def camera_report(camera):
    left, right, bottom, top, near, far = camera["planes"]
    fov = [math.degrees(math.atan(b / near) - math.atan(a / near))
           for a, b in ((left, right), (bottom, top))] if camera["type"] == "PERSP" else None
    record = camera["source_record"]
    return {"camera": camera["name"], "camera_id": camera["id"],
            "matrix_world": camera["matrix"],
            "resolution": camera["resolution"], "fov_xy_deg": fov, "projection": camera["type"],
            "clip_m": [near, far], "lens_mm": record["lens_mm"], "sensor_fit": record["sensor_fit"],
            "pixel_aspect": record["pixel_aspect"], "shift": [record["shift_x"], record["shift_y"]]}


def game_camera(eye_cm, target_cm, fov_deg, resolution, *, roll_deg=0, clip_cm=None, ortho_height_cm=None):
    eye, target = (np.asarray(game_to_view(coords)) for coords in (eye_cm, target_cm))
    if not np.isfinite([eye, target]).all() or np.array_equal(eye, target) or not 0 < fov_deg < 180:
        raise ValueError("Expected distinct finite eye and target coordinates and a vertical FOV between 0 and 180 degrees")
    distance = math.dist(eye, target)
    back = (eye - target) / distance
    up = [0, 0, -1] if math.hypot(back[0], back[2]) < 1e-8 else [0, 1, 0]
    right = np.cross(up, back)
    right /= np.linalg.norm(right)
    up = np.cross(back, right)
    roll = math.radians(roll_deg)
    c, s = math.cos(roll), math.sin(roll)
    world = IDENTITY.copy()
    world[:3, :3] = np.column_stack([c * right + s * up, c * up - s * right, back])
    world[:3, 3] = eye
    near, far = (value / 100 for value in clip_cm) if clip_cm is not None else (.01, max(3000., distance * 4))
    ortho = ortho_height_cm is not None
    half_height = ortho_height_cm / 200 if ortho else near * math.tan(math.radians(fov_deg / 2))
    half_width = half_height * resolution[0] / resolution[1]
    return {"id": "manual", "name": "manual", "type": "ORTHO" if ortho else "PERSP", "matrix": world.tolist(),
            "resolution": list(resolution), "planes": [-half_width, half_width, -half_height, half_height, near, far]}


def reference_scene(path, project, scene_name=None, island=None, origin=50000):
    from .ugc_geometry import source_geometry

    info, objects, cameras, _, arrays = source_geometry(path, scene_name)
    alignment, ratio = alignment_matrix(project, info["units"]["meters_per_unit"])
    scene = new_scene("reference", info["name"], island=island, origin=origin)
    for item in objects:
        if not item["triangle_count"]:
            continue
        start, first = item["vertex_start"], item["triangle_start"]
        vertices = slice(start, start + item["vertex_count"])
        faces = slice(first, first + item["triangle_count"])
        resource = model(apply_matrix(arrays["verts"][vertices], legacy_authoring_to_view(origin) @ alignment),
                         arrays["triangles"][faces] - start, arrays["triangle_uvs"][faces])
        scene.add(resource, IDENTITY, id=item["id"], name=item["name"],
                  references=list(dict.fromkeys(key for key in (item["id"], item["name"], item["instancer"]) if key)))
    scene.manifest.update(bounds=scene.bounds(), active_camera=info["active_camera"],
                          source_sha256=file_sha256(path), source_to_authoring_m=alignment.tolist(),
                          cameras=[source_camera(record, alignment, ratio, origin) for record in cameras
                                   if record["type"] in {"PERSP", "ORTHO"}])
    return scene


def texture_scene(scene, textures, candidates, bindings, *, meshes=None):
    """Attach only existing archive entries, retaining the evidence for each binding."""
    from contextlib import nullcontext
    import json
    from zipfile import ZipFile

    from .asset_bundle import IMAGE_SUFFIXES, pack_materials
    from .catalog_assets import _materials

    resources = {}
    images = {}
    with (ZipFile(textures or meshes) if textures or meshes else nullcontext()) as archive:
        packed = pack_materials(archive)
        for item in scene.manifest["instances"]:
            key = item["model"]
            if key is None:
                continue
            oid = item["objectId"]
            pair = (key, oid)
            if pair not in resources:
                resource = scene.models[key]
                names = list(dict.fromkeys(section["material"] for section in resource.asset["sections"]))
                choices = {row["source_material"]: row for row in candidates.get(str(oid), ())}
                evidence = _materials(names, choices, bindings)
                for row in evidence:
                    saved = packed.get(oid, {}).get(row["material"])
                    if saved and row["binding"] != "confirmed":
                        row.update(saved)
                    if saved:
                        row["alpha_test"] = saved["alpha_test"]
                    name = row["texture"]
                    if not name:
                        continue
                    extension = Path(name).suffix.lower()
                    if (textures or packed) and name in archive.NameToInfo and extension in IMAGE_SUFFIXES:
                        if name not in images:
                            data = archive.read(name)
                            url = f"/blobs/{sha256(data).hexdigest()}{extension}"
                            scene.blobs[url] = data
                            images[name] = url
                        row["url"] = images[name]
                        row.pop("image_issue", None)
                    else:
                        row["image_issue"] = "missing_image"
                new_key = sha256((key + json.dumps(evidence, sort_keys=True)).encode()).hexdigest()
                scene.models[new_key] = resource
                scene.manifest["models"][new_key] = {**resource.asset, "materials": evidence}
                resources[pair] = new_key
            item["model"] = resources[pair]
    used = {row["model"] for row in scene.manifest["instances"]}
    scene.manifest["models"] = {key: value for key, value in scene.manifest["models"].items() if key in used}
    scene.models = {key: value for key, value in scene.models.items() if key in used}
    scene.manifest["mode"] = "textured"
