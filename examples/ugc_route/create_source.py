"""Generate a small original Blender scene for route reconstruction and QA."""
import math
from pathlib import Path
import sys

import bpy
from mathutils import Vector


def material(name, color):
    result = bpy.data.materials.new(name)
    result.diffuse_color = (*color, 1)
    return result


def box(name, position, size, surface):
    bpy.ops.mesh.primitive_cube_add(size=1, location=position)
    obj = bpy.context.object
    obj.name = name
    obj.dimensions = size
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    obj.data.materials.append(surface)
    return obj


def camera(name, position, target):
    data = bpy.data.cameras.new(name)
    data.lens = 32
    data.sensor_fit = "HORIZONTAL"
    obj = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(obj)
    obj.location = position
    obj.rotation_euler = (Vector(target) - obj.location).to_track_quat("-Z", "Y").to_euler()
    return obj


def gate_scene(scene):
    stone = material("Concrete", (.52, .57, .62))
    road = material("Asphalt", (.18, .21, .25))
    metal = material("Blue steel", (.18, .37, .55))
    wood = material("Bench timber", (.52, .31, .16))
    box("Road", (0, 8, -.25), (10, 36, .5), road)
    box("Gate west post", (-4, 7, 2), (.6, .65, 4), stone)
    box("Gate east post", (4, 7, 2), (.6, .65, 4), stone)
    box("Gate header", (0, 7, 4), (8.6, .65, .6), metal)
    box("Tower", (-8, 10, 5), (2.5, 2.5, 10), metal)
    box("Tower cap", (-8, 10, 10), (3.1, 3.1, .55), stone)
    box("Workshop", (7.5, 13, 1.4), (4, 6, 2.8), stone)
    section = box("Array facade", (6.2, 10.7, 1.5), (.65, .65, 3), metal)
    array = section.modifiers.new("Three sections", "ARRAY")
    array.count = 3
    array.relative_offset_displace = (0, 2, 0)

    bench = bpy.data.collections.new("Bench prototype")
    for name, position, size in [
        ("Bench seat", (0, 0, .45), (2, .65, .12)),
        ("Bench back", (0, .28, .74), (2, .12, .45)),
        ("Bench legs", (0, 0, .22), (1.55, .29, .4)),
    ]:
        part = box(name, position, size, wood)
        part.users_collection[0].objects.unlink(part)
        bench.objects.link(part)
    for name, position, yaw in [("Bench west", (-5.8, 5, 0), 0),
                                ("Bench east", (5.8, 5, 0), math.pi)]:
        instance = bpy.data.objects.new(name, None)
        instance.instance_type = "COLLECTION"
        instance.instance_collection = bench
        instance.location = position
        instance.rotation_euler.z = yaw
        scene.collection.objects.link(instance)

    scene.camera = camera("Approach", (0, -8, 1.7), (0, 10, 3.25))
    camera("Alongside", (.4, 1, 1.7), (-3.5, 11, 3.5))
    camera("Exit", (0, 8.5, 1.7), (0, 22, 2.4))

def overpass_scene(scene):
    concrete = material("Concrete", (.52, .57, .62))
    ground = material("Lower lane", (.26, .29, .33))
    rubber = material("Rubber", (.09, .1, .12))
    box("Deck", (0, 2, 5.8), (10, 16, .4), concrete)
    for name, y in (("Support A", 1), ("Support B", 7)):
        box(name, (0, y, 2.8), (3.4, 1.9, 5.6), concrete)
    box("Lower road", (2, 7, -.15), (16, 15, .3), ground)
    bpy.ops.mesh.primitive_torus_add(location=(3, 7, .7), major_radius=.36, minor_radius=.1,
                                     rotation=(0, math.pi / 2, 0))
    bpy.context.object.name = "Visible tire"
    bpy.context.object.data.materials.append(rubber)
    scene.camera = camera("Lower drive", (8, 7, 1.7), (1.4, 6.5, 1.45))
    camera("Upper approach", (0, -3, 7.7), (0, 8, 5.8))
    camera("Neighboring branch", (-9, 7, 4.5), (0, 5, 3.5))


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.render.resolution_x, scene.render.resolution_y = 720, 405
    scene.render.resolution_percentage = 100
    args = sys.argv[sys.argv.index("--") + 1:]
    if "--overpass" in args:
        overpass_scene(scene)
    else:
        gate_scene(scene)
    output = Path(args[0]).resolve()

    output.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output), compress=True)


if __name__ == "__main__":
    main()
