"""Build good and intentionally shifted versions of an original gate approach."""
from copy import deepcopy
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.dont_write_bytecode = True

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

from ugc_project import (approve_road, build, instance, new_project, place,
                         rotate_assembly, save_json, transform)


def route_project():
    project = new_project("Gate approach")
    entries = [
        ("road-before", 16197, (0, -10, 0), -90, (1, 1, 1), "Pavement", "road", "Road"),
        ("road-after", 16197, (0, 6, 0), -90, (1, 1, 1), "Pavement", "road", "Road"),
        ("gate-west", 12005, (-3.9, 6.85, 1.48), 0, (1.1, 1.1, 1.35), "Gate", "near", "Gate west post"),
        ("gate-east", 12005, (4.1, 6.85, 1.48), 0, (1.1, 1.1, 1.35), "Gate", "near", "Gate east post"),
        ("gate-header", 13003, (0, 7, 3.91), 0, (4.9, .21, 4), "Gate", "near", "Gate header"),
        ("tower", 10048, (-8, 10, 1.05), 0, (.78, .78, .35), "Tower", "far", "Tower"),
        ("workshop", 10053, (7.05, 12.1, .98), 90, (.86, .88, .55), "Workshop", "near", "Workshop"),
        ("facade-1", 12005, (5.96, 10.7, 1.11), 0, (1, 1, 1), "Workshop facade", "near", "Array facade"),
        ("facade-2", 12005, (5.96, 12, 1.11), 0, (1, 1, 1), "Workshop facade", "near", "Array facade"),
        ("facade-3", 12005, (5.96, 13.3, 1.11), 0, (1, 1, 1), "Workshop facade", "near", "Array facade"),
        ("bench-west", 16052, (-5.8, 5, 0), 0, (1, 1, 1), "West bench", "shoulder", "Bench west"),
        ("bench-east", 16052, (5.8, 5, 0), 0, (1, 1, 1), "East bench", "shoulder", "Bench east"),
    ]
    for key, oid, position, yaw, scale, assembly, zone, source in entries:
        place(project, key, oid, transform(position, yaw, scale), assembly, zone, source)
    approve_road(project)
    project["landmarks"] = [{"name": "Tower", "source": ["Tower", "Tower cap"], "result": ["tower"],
                             "cameras": ["Approach", "Alongside"]}]
    project["road_joins"] = [{"from": "road-before", "to": "road-after", "at_m": [0, 6, 0],
                              "forward_xy": [0, 1], "half_width_m": 4,
                              "sample_distance_m": .12, "height_tolerance_m": .05,
                              "planar_tolerance_m": .01,
                              "drive_material": dict.fromkeys(("from", "to"), "MI_Neon_Road_1V_Proxy")}]
    return project


def overpass_project():
    project = new_project("Overpass visibility fixture")
    place(project, "deck", 16197, transform((0, -6, 6), -90), "Deck", "road")
    lane = place(project, "lower-lane", 13003, transform((2, 7, -.17), 0, (9.15, 4.55, 2.6)),
                 "Lower lane", "shoulder", "Lower road", "route")
    lane["gameplay"] = True
    for key, y in (("support-a", 1), ("support-b", 7)):
        place(project, key, 16180, transform((0, y, 0), 0, (1, 1, .40)), "Supports", "near", role="support")
    for key, point in (("tire-inside", (0, 1, 1.7)), ("tire-visible", (3, 7, .7))):
        entry = place(project, key, 16087, transform(point), "Tires", "near", role="decor")
        entry["gameplay"] = False
    approve_road(project)
    project["opaque_instances"] = ["support-a", "support-b", "lower-lane"]
    project["landmarks"] = [{"name": "Visible tire", "source": ["Visible tire"], "result": ["tire-visible"],
                             "cameras": ["Lower drive"]}]
    project["viewpoints"] = [
        {"name": "Upper approach", "route": "upper", "at_m": [0, -3, 7.7]},
        {"name": "Neighboring branch", "route": "neighbor", "at_m": [-9, 7, 4.5]},
        {"name": "Lower road", "route": "lower", "at_m": [8, 7, 1.7]},
    ]
    return project


def main():
    output = Path(sys.argv[1])
    output.mkdir(parents=True, exist_ok=True)
    project = route_project()
    build(project, output / "before-bench-edit.pugc")
    rotate_assembly(project, "East bench", (5.8, 5, 0), 180)
    save_json(output / "route.json", project)
    build(project, output / "route.pugc", output / "route-game.json")
    wrong = deepcopy(project)
    instance(wrong, "tower")["transform"]["translation"]["x"] = 800
    build(wrong, output / "wrong-tower.pugc")
    overpass = overpass_project()
    save_json(output / "overpass.json", overpass)
    build(overpass, output / "overpass.pugc")
    cleaned = deepcopy(overpass)
    cleaned["instances"].remove(instance(cleaned, "tire-inside"))
    save_json(output / "overpass-clean.json", cleaned)
    build(cleaned, output / "overpass-clean.pugc")


if __name__ == "__main__":
    main()
