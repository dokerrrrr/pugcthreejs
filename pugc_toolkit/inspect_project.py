"""Inspect a project's revision, placement budget and selected instances."""
from hashlib import sha256
import json
from pathlib import Path

from . import cli_protocol as cli
from .game_space import SPACE, TRANSFORM
from .resources import PROFILE
from .ugc_project import _game_records, load_json, xyz
from .ugc_validation import ZONES, budget_rows, finite, project_errors


def read_project(path):
    data = Path(path).read_bytes()
    project = json.loads(data)
    errors = project_errors(project)
    if errors:
        raise cli.CliError(cli.INVALID, "invalid_project", errors[0], "Validate the project for details.", path=path)
    return project, sha256(data).hexdigest()


def project_budget(project, profile):
    entries = project["instances"]
    return [{key: row[key] for key in ("key", "label", "count", "limit", "remaining", "complete") if key in row}
            for row in budget_rows(_game_records(project)["objects"], load_json(profile),
                                   [entry["zone"] for entry in entries])]


def inspect(path, *, instance_id=None, assembly=None, zone=None, bounds=None,
            limit=20, offset=0, profile=PROFILE, geometry=None, terrain=False):
    if not 1 <= limit <= 100 or offset < 0:
        raise ValueError("Expected a limit from 1 to 100 and a nonnegative offset")
    if bounds is not None and (len(bounds) != 6 or not all(finite(v) for v in bounds)
                               or any(lo > hi for lo, hi in zip(bounds[:3], bounds[3:]))):
        raise ValueError("Expected finite bounds ordered as xmin ymin zmin xmax ymax zmax")
    project, revision = read_project(path)
    entries = project["instances"]
    game = _game_records(project)
    matches, omissions = [], []
    selected = any(value is not None for value in (instance_id, assembly, zone, bounds))
    for entry, record in zip(entries, game["objects"], strict=True) if selected else ():
        if any(value is not None and entry[key] != value
               for key, value in (("id", instance_id), ("assembly", assembly), ("zone", zone))):
            continue
        transform = record[TRANSFORM]
        position = xyz(transform["translation"])
        row = {**{key: entry[key] for key in ("id", "objectId", "assembly", "zone")}, TRANSFORM: transform}
        if geometry:
            from .placement_geometry import world_vertices

            try:
                vertices = world_vertices(geometry.mesh(row), transform)
                low, high = vertices.min(0).tolist(), vertices.max(0).tolist()
                row["bounds_cm"] = [low, high]
            except cli.CliError as exc:
                if exc.exit_code != cli.RESOURCE:
                    raise
                row["geometry_unavailable"] = exc.diagnostic["code"]
                low = high = position
        else:
            low = high = position
        if bounds and not all(a <= q and p <= b for a, b, p, q in zip(bounds[:3], bounds[3:], low, high)):
            if bounds and row.get("geometry_unavailable"):
                omissions.append({"id": row["id"], "objectId": row["objectId"], "reason": row["geometry_unavailable"]})
            continue
        matches.append(row)
    placements = matches[offset:offset + limit]
    if terrain:
        ground = []
        for row in placements:
            x, y, _ = xyz(row[TRANSFORM]["translation"])
            try:
                ground.append({"id": row["id"], "terrain_z_cm": geometry.height("terrain", x, y)})
            except cli.CliError as exc:
                if exc.exit_code != cli.RESOURCE:
                    raise
                ground.append({"id": row["id"], "geometry_unavailable": exc.diagnostic["code"]})
        for row, details in zip(placements, ground, strict=True):
            row["ground"] = details
    for row in placements:
        if reason := row.get("geometry_unavailable") or row.get("ground", {}).get("geometry_unavailable"):
            omissions.append({"id": row["id"], "objectId": row["objectId"], "reason": reason})
    return {"name": project["game"].get("name"), "project_sha256": revision,
            "coordinate_space": SPACE, "project_schema": project["schema"],
            "instances": len(entries), "assemblies": len({entry["assembly"] for entry in entries}),
            "budget": project_budget(project, profile), "matches": len(matches), "water_z_cm": 0,
            "bounds_method": "mesh_aabb" if geometry else "pivots",
            "placements": placements, "coverage": "partial" if omissions else "complete",
            **({"missing_geometry": omissions[:limit], "missing_geometry_count": len(omissions)} if omissions else {})}


def add_arguments(parser):
    parser.add_argument("project", type=Path)
    parser.add_argument("--id")
    parser.add_argument("--assembly")
    parser.add_argument("--zone", choices=ZONES)
    parser.add_argument("--bounds", type=float, nargs=6, metavar=("XMIN", "YMIN", "ZMIN", "XMAX", "YMAX", "ZMAX"))
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--meshes", type=Path, help="Include native mesh bounds and use them for area selection")
    parser.add_argument("--terrain", action="store_true", help="Sample terrain at returned placement pivots")
    parser.add_argument("--base-level", type=Path)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--profile", type=Path, default=PROFILE)


def run(args):
    try:
        geometry = None
        if args.meshes or args.terrain:
            from .placement_geometry import PlacementGeometry
            from .resources import MESHES

            geometry = PlacementGeometry(args.meshes or MESHES, args.base_level)
        data = inspect(args.project, instance_id=args.id, assembly=args.assembly, zone=args.zone,
                       bounds=args.bounds, limit=args.limit, offset=args.offset, profile=args.profile,
                       geometry=geometry, terrain=args.terrain)
        diagnostics = [cli.diagnostic(row["reason"], "Geometry coverage is incomplete.",
                                      "Supply the mesh or terrain resource.", instance_id=row["id"],
                                      object_id=row["objectId"]) for row in data.get("missing_geometry", ())]
        result = cli.response(data, diagnostics=diagnostics, exit_code=cli.RESOURCE if diagnostics else 0)
    except cli.INPUT_ERRORS as exc:
        result = cli.from_error(exc, path=args.project, code="invalid_project")
    if args.json:
        return result
    print(json.dumps(result["data"] if result["exit_code"] == 0 else result["diagnostics"],
                     ensure_ascii=False, indent=2))
    return result["exit_code"]
