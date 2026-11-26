"""Add the pugc-editor island and a static water surface to a Blender scene."""
import bpy
from mathutils import Matrix, Vector

from .base_level import ROCK_COLOR, TERRAIN_COLOR, WATER_BOUNDS_M, WATER_COLOR, WATER_Z_M, environment_metadata

COLLECTION_NAME = "PUGC base level"


def add_base_level(scene, filepath, *, origin_x_cm=None):
    # The importer maps glTF (X, up, map Y) to Blender (X, -map Y, up).
    # Raw PUGC uses (X, map Y, up). Authoring also reflects X about the receipt's origin.
    raw_to_scene = Matrix.Identity(4)
    if origin_x_cm is not None:
        raw_to_scene @= Matrix.Translation((2 * origin_x_cm / 100, 0, 0))
        raw_to_scene @= Matrix.Diagonal((-1, 1, 1, 1))
    gltf_to_scene = raw_to_scene @ Matrix.Diagonal((1, -1, 1, 1))

    group = bpy.data.collections.new(COLLECTION_NAME)
    scene.collection.children.link(group)
    for name, value in environment_metadata().items():
        group[name] = value
    existing = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(filepath))
    imported = set(bpy.data.objects) - existing
    for obj in sorted(imported, key=lambda item: item.name):
        matrix_world = obj.matrix_world.copy()
        for collection in list(obj.users_collection):
            collection.objects.unlink(obj)
        group.objects.link(obj)
        if obj.parent not in imported:
            obj.matrix_world = gltf_to_scene @ matrix_world
        obj.color = (*(TERRAIN_COLOR if obj.name.startswith("terrain_") else ROCK_COLOR), 1)
        obj.select_set(False)

    x0, y0, x1, y1 = WATER_BOUNDS_M
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    faces = [(0, 3, 2, 1)] if origin_x_cm is not None else [(0, 1, 2, 3)]
    mesh = bpy.data.meshes.new("PUGC water surface")
    mesh.from_pydata([raw_to_scene @ Vector((x, y, WATER_Z_M)) for x, y in corners], [], faces)
    mesh.update()
    water = bpy.data.objects.new("Water", mesh)
    group.objects.link(water)
    water.color = (*WATER_COLOR, 1)
    material = bpy.data.materials.new("Water preview")
    material.diffuse_color = water.color
    material.use_nodes = True
    shader = material.node_tree.nodes.get("Principled BSDF")
    shader.inputs["Base Color"].default_value = water.color
    shader.inputs["Roughness"].default_value = .34
    mesh.materials.append(material)
    return group
