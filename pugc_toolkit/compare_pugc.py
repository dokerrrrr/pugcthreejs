"""Render Blender source NPZ and a re-decoded PUGC from the same route cameras."""
import argparse
import base64
from contextlib import nullcontext
import hashlib
from math import atan, degrees
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import bpy
from bpy_extras.object_utils import world_to_camera_view
from mathutils import Matrix, Vector
import numpy as np

from . import cli_protocol as cli
from . import geometry_report as coverage
from .base_level import add_arguments, environment_metadata, resolve_base_level
from .blender_base_level import add_base_level
from .mesh_library import MeshLibrary
from .resources import CATALOG, MESHES
from .ugc_geometry import apply_matrix, placement_matrix, source_geometry
from .ugc_project import load_json, read_with_receipt, save_json


def material(name, color, path=None, packed=None):
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = color
    mat.use_nodes = True
    node = mat.node_tree.nodes.get("Principled BSDF")
    node.inputs["Base Color"].default_value = color
    node.inputs["Roughness"].default_value = .8
    image = None
    issue = None
    if path or packed:
        try:
            if packed:
                with TemporaryDirectory() as temporary:
                    image_file = Path(temporary) / "source-image"
                    image_file.write_bytes(base64.b64decode(packed))
                    image = bpy.data.images.load(str(image_file))
                    image.pack()
            else:
                image = bpy.data.images.load(str(path), check_existing=True)
            if not all(image.size):
                issue = "missing_image"
            elif tuple(image.size) == (1, 1):
                issue = "possible_placeholder_1x1"
            else:
                tex = mat.node_tree.nodes.new("ShaderNodeTexImage")
                tex.image = image
                mat.node_tree.links.new(tex.outputs["Color"], node.inputs["Base Color"])
        except (RuntimeError, OSError):
            issue = "missing_image"
    return mat, issue


def mesh(name, vertices, triangles, uv, materials, indices):
    data = bpy.data.meshes.new(name)
    data.from_pydata(vertices.tolist(), [], triangles.tolist())
    for item in materials:
        data.materials.append(item)
    if indices is not None and len(triangles):
        data.polygons.foreach_set("material_index", np.asarray(indices, dtype=np.int32))
    if uv is not None and len(triangles):
        layer = data.uv_layers.new(name="UVMap")
        layer.data.foreach_set("uv", uv.astype(np.float32).ravel())
    data.update()
    return data


def result_material(oid, name, textures, matches, confirmed, directory, report):
    candidate = next((entry.get("diffuse_guess") for entry in matches.get(str(oid), [])
                      if entry["source_material"] == name), None)
    path = confirmed.get(name) or candidate
    status = "confirmed" if name in confirmed else "name_similarity" if candidate else "missing_binding"
    report_row = {"objectId": oid, "material": name, "texture": path, "binding": status}
    local = None
    if path:
        if textures is not None and path in textures.NameToInfo:
            local = directory / (hashlib.sha256(path.encode()).hexdigest() + Path(path).suffix)
            if not local.exists():
                local.write_bytes(textures.read(path))
        else:
            report_row["image_issue"] = "missing_image"
    mat, issue = material(name, (.45, .48, .52, 1), local)
    if issue:
        report_row["image_issue"] = issue
    report.append(report_row)
    return mat


def import_source(group, objects, slots, arrays, alignment, neutral, issues):
    assigned = []
    for item in slots:
        if neutral or item is None:
            assigned.append(neutral or material("Unassigned", (.45, .48, .52, 1))[0])
            continue
        image = item.get("image")
        mat, issue = material(item["name"], item["color"], image.get("path") if image else None,
                              image.get("packed") if image else None)
        if image:
            status = issue or ("missing_image" if not (image.get("path") or image.get("packed")) else None)
            issues.append({"material": item["name"], "image": image["name"], "issue": status})
        assigned.append(mat)
    references = {}
    for item in objects:
        first, start = item["vertex_start"], item["triangle_start"]
        vs = slice(first, first + item["vertex_count"])
        fs = slice(start, start + item["triangle_count"])
        if not item["triangle_count"]:
            continue
        points = apply_matrix(arrays["verts"][vs], alignment)
        faces = arrays["triangles"][fs] - first
        source_uv = arrays["triangle_uvs"][fs] if not neutral and item["uv_layer"] else None
        data = mesh(item["name"], points, faces, source_uv,
                    [assigned[i] for i in item["material_slots"]],
                    arrays["materials"][fs] - item["material_slots"][0])
        obj = bpy.data.objects.new(item["name"], data)
        group.objects.link(obj)
        for key in {item["id"], item["name"], item["instancer"]} - {None}:
            references.setdefault(key, []).append((obj, points, faces))
    return references


def import_result(group, data, receipt, library, neutral, textures, matches, confirmed, directory, report, tracked):
    meshes, refs = {}, {}
    for entry, record in zip(data["objects"], receipt["instances"], strict=True):
        oid, key = entry["objectId"], record["id"]
        if oid not in library.ids:
            report["missing_meshes"].append({"instance": key, "objectId": oid})
            continue
        if oid not in meshes:
            geo = library.geometry(oid)
            if neutral:
                mats, indices = [neutral], None
            else:
                mats = [material("Unassigned", (.45, .48, .52, 1))[0]]
                indices = np.zeros(len(geo.triangles), dtype=np.int32)
                assigned = {}
                for section in geo.sections:
                    if section.material not in assigned:
                        assigned[section.material] = len(mats)
                        mats.append(result_material(oid, section.material, textures, matches, confirmed,
                                                    directory, report["result_materials"]))
                    first = section.first_triangle
                    indices[first:first + section.triangle_count] = assigned[section.material]
            meshes[oid] = mesh(f"UGC {oid}", geo.vertices, geo.triangles,
                               None if neutral else geo.triangle_uvs, mats, indices), geo.vertices, geo.triangles
        model, vertices, faces = meshes[oid]
        matrix = placement_matrix(entry["spawnTransform"])
        obj = bpy.data.objects.new(f"{key} / {oid}", model)
        group.objects.link(obj)
        obj.matrix_world = Matrix(matrix.tolist())
        if key in tracked:
            refs[key] = [(obj, apply_matrix(vertices, matrix), faces)]
    return refs


def configure_camera(scene, camera, record, alignment, ratio):
    if record["type"] not in {"PERSP", "ORTHO"}:
        raise ValueError(f"Camera {record['name']} requires perspective or orthographic projection")
    matrix = alignment @ np.asarray(record["matrix_world"])
    matrix[:3, :3] /= np.linalg.norm(matrix[:3, :3], axis=0)
    camera.matrix_world = Matrix(matrix.tolist())
    data = camera.data
    data.type = record["type"]
    data.lens, data.sensor_fit = record["lens_mm"], record["sensor_fit"]
    data.sensor_width, data.sensor_height = record["sensor_width"], record["sensor_height"]
    data.shift_x, data.shift_y = record["shift_x"], record["shift_y"]
    data.ortho_scale = record["ortho_scale"] * ratio
    data.clip_start, data.clip_end = record["clip_start"] * ratio, record["clip_end"] * ratio
    scene.render.resolution_x, scene.render.resolution_y = record["resolution"]
    scene.render.pixel_aspect_x, scene.render.pixel_aspect_y = record["pixel_aspect"]
    bpy.context.view_layer.update()


def observe(scene, camera, refs):
    if not refs:
        return {"side": "absent", "visible": False}
    bounds = np.concatenate([points for _, points, _ in refs])
    center = Vector((bounds.min(axis=0) + bounds.max(axis=0)) / 2)
    coordinate = world_to_camera_view(scene, camera, center)
    graph = bpy.context.evaluated_depsgraph_get()
    allowed = {obj for obj, _, _ in refs}
    visible = False
    for _, points, faces in refs:
        samples = points[faces[np.linspace(0, len(faces)-1, min(len(faces), 28), dtype=int)]].mean(axis=1)
        for point in samples:
            target = Vector(point)
            projected = world_to_camera_view(scene, camera, target)
            if not (camera.data.clip_start <= projected.z <= camera.data.clip_end
                    and 0 <= projected.x <= 1 and 0 <= projected.y <= 1):
                continue
            origin = camera.matrix_world.translation.copy()
            if camera.data.type == "ORTHO":
                camera_point = camera.matrix_world.inverted() @ target
                origin = camera.matrix_world @ Vector((camera_point.x, camera_point.y, 0))
            ray = target - origin
            hit, _, _, _, obj, _ = scene.ray_cast(graph, origin, ray.normalized(), distance=ray.length + .01)
            if hit and obj.original in allowed:
                visible = True
                break
        if visible:
            break
    return {"side": "left" if coordinate.x < .495 else "right" if coordinate.x > .505 else "center",
            "visible": visible, "projection": [float(coordinate.x), float(coordinate.y)],
            "center_m": list(center)}


def switch_layer(source, result, environment, show_source):
    source.exclude = not show_source
    result.exclude = show_source
    environment.exclude = True
    bpy.context.view_layer.update()


def render(scene, path):
    scene.render.filepath = str(path)
    bpy.ops.render.render(write_still=True)


def flythrough(scene, camera, records, alignment, ratio, output, directory, fps):
    if len(records) < 2 or any(record["type"] != "PERSP" for record in records):
        raise ValueError("Flythrough requires at least two perspective cameras")
    signatures = [{key: record[key] for key in ("resolution", "pixel_aspect", "lens_mm", "sensor_fit",
                                               "sensor_width", "sensor_height", "shift_x", "shift_y")}
                  for record in records]
    if any(sig != signatures[0] for sig in signatures):
        raise ValueError("Flythrough cameras must have matching optics and resolution")
    locations, rotations = [], []
    for record in records:
        configure_camera(scene, camera, record, alignment, ratio)
        locations.append(camera.matrix_world.translation.copy())
        rotations.append(camera.matrix_world.to_quaternion())
    frame = 0
    for i in range(len(records) - 1):
        for t in np.linspace(0, 1, fps, endpoint=False):
            camera.location = locations[i].lerp(locations[i+1], t)
            camera.rotation_mode = "QUATERNION"
            camera.rotation_quaternion = rotations[i].slerp(rotations[i+1], t)
            render(scene, directory / f"flight-{frame:04}.png")
            frame += 1
    camera.location, camera.rotation_quaternion = locations[-1], rotations[-1]
    render(scene, directory / f"flight-{frame:04}.png")
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-framerate", str(fps), "-i",
                    str(directory / "flight-%04d.png"), "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", str(output)], check=True)


def main(*, machine=False):
    parser_type = cli.MachineParser if machine else argparse.ArgumentParser
    parser = parser_type(prog="pugc blender compare" if machine else None, description=__doc__)
    parser.add_argument("source", help="NPZ produced by blender_extract_geometry.py")
    parser.add_argument("pugc")
    parser.add_argument("--project", help="Optional project with landmarks and source_to_authoring_m")
    parser.add_argument("--receipt")
    parser.add_argument("--origin-x-cm", type=float)
    parser.add_argument("--scene")
    parser.add_argument("--cameras", nargs="+", help="Camera names/IDs in race order")
    parser.add_argument("--meshes", default=MESHES)
    parser.add_argument("--textures")
    parser.add_argument("--bindings", help="Confirmed material-path → ZIP-entry JSON")
    parser.add_argument("--mode", choices=["geometry", "textured"], default="geometry")
    parser.add_argument("--out", required=True)
    parser.add_argument("--flythrough", type=Path)
    parser.add_argument("--fps", type=int, default=12)
    parser.add_argument("--samples", type=int, default=18)
    parser.add_argument("--strict", action="store_true", help="Require camera and landmark coverage")
    add_arguments(parser)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    if args.fps < 1 or args.samples < 1:
        parser.error("--fps and --samples must be positive")
    cli.archive_argument(args, parser, "--meshes")
    if args.textures:
        cli.archive_argument(args, parser, "--textures")
    if args.flythrough and shutil.which("ffmpeg") is None:
        if machine:
            raise cli.CliError(cli.RESOURCE, "ffmpeg_missing", "Recording a flythrough requires FFmpeg on PATH.",
                               "Add FFmpeg to PATH or omit --flythrough.", field="--flythrough")
        parser.error("Recording a flythrough requires FFmpeg on PATH.")
    directory = Path(args.out).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    project = load_json(args.project) if args.project else {}
    data, receipt = read_with_receipt(args.pugc, args.receipt, args.origin_x_cm)
    source_scene, objects, cameras, slots, arrays = source_geometry(args.source, args.scene)
    by_key = {record["id"]: record for record in cameras}
    by_key.update({record["name"]: record for record in cameras if not record["instance"]})
    default_key = source_scene["active_camera"] or (cameras[0]["id"] if cameras else "active source camera")
    keys = args.cameras or [default_key]
    selected = {key: by_key[key] for key in keys if key in by_key and by_key[key]["type"] in {"PERSP", "ORTHO"}}
    missing_cameras = [key for key in keys if key not in selected]
    targets = {}
    for key in keys:
        camera_labels = {key}
        if key in by_key:
            camera_labels.update((by_key[key]["id"], by_key[key]["name"]))
        targets[key] = [landmark for landmark in project.get("landmarks", [])
                        if not landmark.get("cameras") or camera_labels.intersection(landmark["cameras"])]
    expected_landmarks = sum(len(targets[key]) for key in keys)
    missing_landmarks = [{"camera": key, "landmark": row["name"], "reason": "camera_unavailable"}
                         for key in missing_cameras for row in targets[key]]
    checked_landmarks = 0
    units = source_scene["units"]["meters_per_unit"]
    alignment = np.array(project.get("source_to_authoring_m", np.eye(4)), dtype=float)
    if alignment.shape != (4, 4) or not np.all(np.isfinite(alignment)) or not np.allclose(alignment[3], [0, 0, 0, 1]):
        raise ValueError("source_to_authoring_m must be a finite affine 4×4 matrix")
    alignment = alignment @ np.diag([units, units, units, 1])
    linear = alignment[:3, :3]
    ratio = np.linalg.norm(linear[:, 0])
    if ratio <= 0 or np.linalg.det(linear) <= 0 or not np.allclose(linear.T @ linear / ratio**2, np.eye(3)):
        raise ValueError("Source alignment requires positive uniform scale and rotation")

    island = resolve_base_level(args, parser)
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    scene.cycles.samples = args.samples
    scene.cycles.use_denoising = False
    scene.render.image_settings.file_format = "PNG"
    scene.render.resolution_percentage = 100
    scene.world = bpy.data.worlds.new("Sky")
    scene.world.use_nodes = True
    scene.world.node_tree.nodes["Background"].inputs[0].default_value = (.37, .48, .64, 1)
    scene.world.node_tree.nodes["Background"].inputs[1].default_value = .45
    light = bpy.data.lights.new("Sun", "SUN")
    light.energy = 2.4
    sun = bpy.data.objects.new("Sun", light)
    scene.collection.objects.link(sun)
    sun.rotation_euler = (.66, -.57, -.2)
    camera = bpy.data.objects.new("Matched camera", bpy.data.cameras.new("Matched camera"))
    scene.collection.objects.link(camera)
    scene.camera = camera
    source = bpy.data.collections.new("Source")
    result = bpy.data.collections.new("Decoded PUGC")
    for collection in (source, result):
        scene.collection.children.link(collection)
    environment = add_base_level(scene, island, origin_x_cm=receipt["origin_x_cm"])
    environment_layer = bpy.context.view_layer.layer_collection.children[environment.name]
    source_layer, result_layer = (bpy.context.view_layer.layer_collection.children[col.name]
                                  for col in (source, result))
    neutral = material("Silhouette", (.57, .62, .68, 1))[0] if args.mode == "geometry" else None
    report = {"pugc_sha256": receipt["pugc_sha256"], "mode": args.mode,
              "comparison_frame": "authoring XYZ metres",
              "source_units_to_authoring_m": alignment.tolist(), "game_origin_x_cm": receipt["origin_x_cm"],
              "environment": environment_metadata(),
              "game_to_authoring": "Reflect world X about the authoring datum, then convert cm to m",
              "frames": [], "issues": [], "source_materials": [], "result_materials": [], "missing_meshes": []}
    mesh_matches = load_json(CATALOG / "texture_material_candidates.json")
    confirmed = load_json(args.bindings) if args.bindings else {}
    source_refs = import_source(source, objects, slots, arrays, alignment, neutral, report["source_materials"])
    with (ZipFile(args.textures) if args.textures else nullcontext()) as textures, TemporaryDirectory() as tmp:
        scratch = Path(tmp)
        tracked = {key for landmark in project.get("landmarks", []) for key in landmark.get("result", [])}
        with MeshLibrary(args.meshes, authoring=True) as library:
            result_refs = import_result(result, data, receipt, library, neutral, textures, mesh_matches,
                                        confirmed, scratch, report, tracked)
        for key in keys:
            if key not in selected:
                continue
            index = len(report["frames"]) + 1
            record = selected[key]
            name = record["name"]
            configure_camera(scene, camera, record, alignment, ratio)
            fov = None
            if record["type"] == "PERSP":
                view = camera.data.view_frame(scene=scene)
                angles = [[atan(point[axis] / -point.z) for point in view] for axis in (0, 1)]
                fov = [degrees(max(values) - min(values)) for values in angles]
            frame = {"camera": name, "camera_id": record["id"], "matrix_world": [list(row) for row in camera.matrix_world],
                     "resolution": record["resolution"], "fov_xy_deg": fov, "projection": record["type"],
                     "clip_m": [camera.data.clip_start, camera.data.clip_end],
                     "lens_mm": record["lens_mm"], "sensor_fit": record["sensor_fit"],
                     "pixel_aspect": record["pixel_aspect"], "shift": [record["shift_x"], record["shift_y"]],
                     "source": f"{index:02}-source.png", "result": f"{index:02}-result.png", "landmarks": []}
            relevant = []
            for landmark in targets[key]:
                source_ids, result_ids = landmark.get("source", []), landmark.get("result", [])
                missing_source = [reference for reference in source_ids if not source_refs.get(reference)]
                missing_result = [reference for reference in result_ids if not result_refs.get(reference)]
                if not source_ids or not result_ids or missing_source or missing_result:
                    missing_landmarks.append({"camera": name, "landmark": landmark["name"],
                                              "reason": "geometry_unavailable",
                                              "missing_source_ids": missing_source, "missing_result_ids": missing_result})
                else:
                    relevant.append(landmark)
            checked_landmarks += len(relevant)
            switch_layer(source_layer, result_layer, environment_layer, True)
            source_sides = [observe(scene, camera, [ref for key in landmark["source"] for ref in source_refs.get(key, [])])
                            for landmark in relevant]
            environment_layer.exclude = False
            render(scene, directory / frame["source"])
            switch_layer(source_layer, result_layer, environment_layer, False)
            for landmark, initial in zip(relevant, source_sides):
                final = observe(scene, camera, [ref for key in landmark["result"] for ref in result_refs.get(key, [])])
                row = {"landmark": landmark["name"], "camera": name, "source": initial, "result": final}
                frame["landmarks"].append(row)
                if initial["visible"] and (initial["side"] != final["side"] or not final["visible"]):
                    issue = "side_changed" if initial["side"] != final["side"] else "lost_visibility"
                    report["issues"].append({**row, "issue": issue})
            environment_layer.exclude = False
            render(scene, directory / frame["result"])
            report["frames"].append(frame)
        missing_ids = [row["instance"] for row in report["missing_meshes"]]
        report["geometry_check"] = coverage.summary(args.strict,
            instances=coverage.check(len(data["objects"]), len(data["objects"]) - len(missing_ids), missing_ids),
            cameras=coverage.check(len(keys), len(report["frames"]), missing_cameras),
            landmarks=coverage.check(expected_landmarks, checked_landmarks, missing_landmarks,
                                     findings=len(report["issues"]), required=args.strict,
                                     requested_item="project.landmarks for selected cameras"),
        )
        save_json(directory / "comparison.json", report)
        if args.flythrough and report["geometry_check"]["status"] == "complete":
            records = [selected[key] for key in keys]
            flythrough(scene, camera, records, alignment, ratio, args.flythrough, scratch, args.fps)
            report["flythrough"] = str(args.flythrough)
            save_json(directory / "comparison.json", report)
    if machine:
        report_path = directory / "comparison.json"
        artifacts = {"report": report_path, "source_frames": [directory / row["source"] for row in report["frames"]],
                     "result_frames": [directory / row["result"] for row in report["frames"]]}
        if "flythrough" in report:
            artifacts["flythrough"] = report["flythrough"]
        diagnostics = [
            cli.missing_mesh(args.meshes, instance_id=row["instance"], object_id=row["objectId"])
            for row in report["missing_meshes"]
        ]
        diagnostics.extend(
            cli.diagnostic("geometry_mismatch", f"{row['issue']}: {row['landmark']} / {row['camera']}",
                           "Inspect the paired frames and correct the landmark placement.",
                           path=report_path, field=f"issues[{index}]")
            for index, row in enumerate(report["issues"])
        )
        diagnostics.extend(coverage.partial_diagnostics(report["geometry_check"], report_path))
        code = coverage.completion(report["geometry_check"])
        return cli.response({"pugc_sha256": receipt["pugc_sha256"], "frames": len(report["frames"]),
                             "geometry_check": report["geometry_check"]},
                            artifacts=artifacts, diagnostics=diagnostics, exit_code=code)
    if args.strict and report["geometry_check"]["status"] == "partial":
        print(f"Partial geometry comparison: {directory / 'comparison.json'}", file=sys.stderr)
        sys.exit(cli.RESOURCE)
    if report["issues"] or report["missing_meshes"] or missing_cameras or missing_landmarks:
        for issue in report["issues"]:
            print(f"{issue['issue']}: {issue['landmark']} / {issue['camera']}")
        sys.exit(2)


if __name__ == "__main__":
    main()
