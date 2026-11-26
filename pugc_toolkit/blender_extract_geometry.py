"""Export evaluated Blender geometry, camera and material metadata to NPZ.

blender -b input.blend --python-exit-code 1 --python tools/blender_extract_geometry.py -- --out scene.npz
"""
import argparse
import base64
import json
import sys
from collections import Counter

import bpy
import numpy as np

from . import cli_protocol as cli

def material_record(material):
    if material is None:
        return None
    record = {"name": material.name, "color": list(material.diffuse_color), "image": None}
    if not material.use_nodes or material.node_tree is None:
        return record
    nodes = material.node_tree.nodes
    output = next((node for node in nodes if node.type == "OUTPUT_MATERIAL"
                   and node.is_active_output), None)
    if not output or not output.inputs["Surface"].links:
        return record
    shader = output.inputs["Surface"].links[0].from_node
    if shader.type != "BSDF_PRINCIPLED":
        return record
    color = shader.inputs["Base Color"]
    record["color"] = list(color.default_value)
    if color.links and color.links[0].from_node.type == "TEX_IMAGE":
        image = color.links[0].from_node.image
        if image is not None:
            record["image"] = {
                "name": image.name,
                "path": bpy.path.abspath(image.filepath, library=image.library) if image.filepath else None,
                "size": list(image.size),
                "status": "source_image",
            }
            if image.packed_file:
                record["image"]["packed"] = base64.b64encode(image.packed_file.data).decode("ascii")
    return record


def extract(scenes):
    vertices, triangles, material_indices, object_indices, loop_uvs = [], [], [], [], []
    objects, materials, cameras, scene_records = [], [], [], []
    vertex_start = triangle_start = 0
    for scene in scenes:
        with bpy.context.temp_override(scene=scene, view_layer=scene.view_layers[0]
                                       if scene != bpy.context.scene else bpy.context.view_layer):
            graph = bpy.context.evaluated_depsgraph_get()
            render = scene.render
            scene_records.append({
                "name": scene.name,
                "view_layer": graph.view_layer.name,
                "units": {"system": scene.unit_settings.system,
                          "length_unit": scene.unit_settings.length_unit,
                          "meters_per_unit": scene.unit_settings.scale_length},
                "active_camera": scene.camera.name if scene.camera else None,
            })
            ids = Counter()
            for instance in graph.object_instances:
                # DepsgraphObjectInstance is only valid during this iteration.
                obj = instance.object
                original = obj.original
                if obj.type not in {"MESH", "CAMERA"}:
                    continue
                if not instance.is_instance and not instance.show_self:
                    continue
                # Blender's UI graph is evaluated with viewport modifier settings.
                parent = instance.parent.original if instance.parent else None
                matrix = np.array(instance.matrix_world, dtype=np.float64)
                persistent = [i for i in instance.persistent_id if i != 2147483647]
                prefix = f"{scene.name}/{parent.name + '/' if parent and instance.is_instance else ''}{original.name}"
                if instance.is_instance:
                    prefix += "@" + ".".join(map(str, persistent))
                ids[prefix] += 1
                record = {
                    "id": prefix if ids[prefix] == 1 else f"{prefix}#{ids[prefix]}",
                    "name": original.name, "scene": scene.name,
                    "collections": sorted(col.name for col in original.users_collection),
                    "instancer": parent.name if instance.is_instance and parent else None,
                    "instance": bool(instance.is_instance), "persistent_id": persistent,
                    "matrix_world": matrix.tolist(),
                }
                if obj.type == "CAMERA":
                    cam = obj.data
                    record.update(type=cam.type, lens_mm=cam.lens, angle_x=cam.angle_x,
                                  angle_y=cam.angle_y, sensor_fit=cam.sensor_fit,
                                  sensor_width=cam.sensor_width, sensor_height=cam.sensor_height,
                                  shift_x=cam.shift_x, shift_y=cam.shift_y, ortho_scale=cam.ortho_scale,
                                  clip_start=cam.clip_start, clip_end=cam.clip_end,
                                  resolution=[max(1, int(v * render.resolution_percentage / 100))
                                              for v in (render.resolution_x, render.resolution_y)],
                                  pixel_aspect=[render.pixel_aspect_x, render.pixel_aspect_y])
                    cameras.append(record)
                    continue
                mesh = obj.to_mesh(preserve_all_data_layers=True, depsgraph=graph)
                if mesh is None:
                    continue
                try:
                    mesh.calc_loop_triangles()
                    coords = np.empty((len(mesh.vertices), 3), dtype=np.float32)
                    mesh.vertices.foreach_get("co", coords.ravel())
                    coords = coords @ matrix[:3, :3].T + matrix[:3, 3]
                    slots = [material_record(slot.material) for slot in obj.material_slots] or [None]
                    material_start = len(materials)
                    materials.extend(slots)
                    uv = mesh.uv_layers.active
                    count = len(mesh.loop_triangles)
                    faces = np.empty((count, 3), dtype=np.int32)
                    face_materials = np.empty(count, dtype=np.int32)
                    texcoords = np.zeros((count, 3, 2), dtype=np.float32)
                    winding = (0, 2, 1) if np.linalg.det(matrix[:3, :3]) < 0 else (0, 1, 2)
                    for i, tri in enumerate(mesh.loop_triangles):
                        faces[i] = [tri.vertices[index] for index in winding]
                        slot = mesh.polygons[tri.polygon_index].material_index
                        face_materials[i] = material_start + (slot if slot < len(slots) else 0)
                        if uv is not None:
                            texcoords[i] = [uv.data[tri.loops[index]].uv for index in winding]
                    vertices.append(coords.astype(np.float32))
                    triangles.append(faces + vertex_start)
                    material_indices.append(face_materials)
                    object_indices.append(np.full(count, len(objects), dtype=np.int32))
                    loop_uvs.append(texcoords)
                    record.update(vertex_start=vertex_start, vertex_count=len(coords),
                                  triangle_start=triangle_start, triangle_count=count,
                                  material_slots=list(range(material_start, len(materials))),
                                  uv_layer=uv.name if uv is not None else None)
                    objects.append(record)
                    vertex_start += len(coords)
                    triangle_start += count
                finally:
                    obj.to_mesh_clear()
    if not vertices:
        raise RuntimeError("No evaluated mesh objects in selected scenes")
    metadata = {"schema": 1, "blender": bpy.app.version_string, "scenes": scene_records,
                "objects": objects, "cameras": cameras, "material_slots": materials}
    return dict(verts=np.concatenate(vertices), triangles=np.concatenate(triangles),
                materials=np.concatenate(material_indices), triangle_objects=np.concatenate(object_indices),
                triangle_uvs=np.concatenate(loop_uvs), metadata=np.array(json.dumps(metadata)))


def main(*, machine=False):
    parser_type = cli.MachineParser if machine else argparse.ArgumentParser
    parser = parser_type(prog="pugc blender extract" if machine else None, description=__doc__)
    parser.add_argument("--out", required=True)
    parser.add_argument("--all-scenes", action="store_true")
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    result = extract(list(bpy.data.scenes) if args.all_scenes else [bpy.context.scene])
    np.savez_compressed(args.out, **result)
    if machine:
        output = args.out if args.out.endswith(".npz") else args.out + ".npz"
        return cli.response({"vertices": len(result["verts"]), "triangles": len(result["triangles"])},
                            artifacts={"source_npz": output})
    print(f"EXPORTED {args.out}: {len(result['verts'])} vertices, {len(result['triangles'])} triangles")


if __name__ == "__main__":
    main()
