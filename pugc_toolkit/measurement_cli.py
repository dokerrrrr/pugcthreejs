"""Command-line terrain and width checks."""
from pathlib import Path
import json
from math import isfinite

from . import cli_protocol as cli
from .resources import MESHES


def add_commands(commands):
    terrain = commands.add_parser("terrain", help="Query island ground height in game centimetres")
    terrain.add_argument("x_cm", type=float)
    terrain.add_argument("y_cm", type=float)
    area = commands.add_parser("area", aliases=["terrain-area"], help="Measure elevation over a rectangle on a map")
    area.add_argument("bounds_cm", type=float, nargs=4, metavar="CM", help="XMIN YMIN XMAX YMAX in game centimetres")
    area.add_argument("--out", type=Path, help="Write a top-down PNG showing elevation")
    area.add_argument("--map", type=Path, help="Project JSON or built PUGC")
    area.add_argument("--receipt", type=Path, help="Placement metadata for a built PUGC")
    area.add_argument("--meshes", type=Path, default=MESHES)
    area.add_argument("--surface", choices=("terrain", "placements", "top"),
                      help="Island ground, native placements, or the highest surface across both")
    area.add_argument("--no-island", action="store_true", help="Use placed geometry across the map")
    area.add_argument("--ids", nargs="+", help="Limit placed geometry to these instance IDs")
    area.add_argument("--z-range-cm", type=float, nargs=2, metavar=("MIN", "MAX"), help="Consider hits in this Z band")
    area.add_argument("--samples", type=int, help="Samples along the longer axis (default 128 for map geometry)")
    area.add_argument("--grid", action="store_true", help="Include the sampled XY axes and Z grid in JSON")
    for command in (terrain, area):
        command.add_argument("--base-level", type=Path)
        command.add_argument("--offline", action="store_true", help="Use only the cached or supplied island")
    measure = commands.add_parser("measure-gap", help="Check passage width in a written map")
    measure.add_argument("pugc", type=Path)
    measure.add_argument("anchor")
    measure.add_argument("other")
    measure.add_argument("--axis", choices=("+X", "-X", "+Y", "-Y"), default="+X")
    measure.add_argument("--expected-cm", type=float)
    measure.add_argument("--tolerance-cm", type=float, default=1)
    measure.add_argument("--support", choices=("terrain", "water"))
    measure.add_argument("--receipt", type=Path)
    measure.add_argument("--meshes", type=Path, default=MESHES)
    measure.add_argument("--base-level", type=Path)
    measure.add_argument("--out", type=Path)


def run(args):
    try:
        from .placement_geometry import PlacementGeometry, terrain_path, terrain_height, verify_gap
        from .ugc_project import save_json

        artifacts = {}
        if args.command == "terrain":
            height = terrain_height(terrain_path(args.base_level, offline=args.offline), args.x_cm, args.y_cm)
            result = cli.response({"position_cm": [args.x_cm, args.y_cm], "terrain_z_cm": height,
                                   "water_z_cm": 0, "coverage": "complete"})
        elif args.command in {"area", "terrain-area"}:
            from .terrain_area import sampled_area, terrain_area, terrain_bounds
            from .ugc_storage import atomic_write, lock_outputs, output_paths

            bounds = terrain_bounds(args.bounds_cm)
            output = args.out
            if output is not None and output.suffix.lower() != ".png":
                raise ValueError("Choose a PNG file for the top view")
            surface = args.surface or ("placements" if args.no_island else "top" if args.map else "terrain")
            if (surface != "terrain" and args.map is None or surface == "terrain" and args.no_island
                    or args.ids and surface == "terrain"):
                raise ValueError("Use --map for placed geometry and an island source for terrain")
            if args.receipt is not None and (args.map is None or args.map.suffix.lower() != ".pugc"):
                raise ValueError("--receipt requires a .pugc source")
            if args.samples is not None and not 2 <= args.samples <= 1024:
                raise ValueError("--samples must be between 2 and 1024")
            z_range = args.z_range_cm
            if z_range is not None and (not all(map(isfinite, z_range)) or z_range[0] > z_range[1]):
                raise ValueError("Expected finite Z bounds ordered MIN MAX")
            island = terrain_path(args.base_level, offline=args.offline) if surface != "placements" and not args.no_island else None
            diagnostics = []
            if surface == "terrain" and not (args.grid or args.samples is not None or z_range is not None):
                data, png = terrain_area(island, bounds, top_view=output is not None)
                data.update(surface="terrain", method="clipped_terrain_triangles")
            else:
                data, png, diagnostics = sampled_area(
                    bounds, island=island, source=args.map if surface != "terrain" else None, meshes=args.meshes,
                    receipt=args.receipt, ids=set(args.ids) if args.ids else None, samples=args.samples or 128,
                    z_range=z_range or (-float("inf"), float("inf")), surface=surface,
                    top_view=output is not None, grid=args.grid)
                if z_range is not None:
                    data["z_range_cm"] = z_range
                if args.ids:
                    data["ids"] = args.ids
            if png is not None:
                output.parent.mkdir(parents=True, exist_ok=True)
                sidecar = args.map.with_suffix(".pugc.json") if args.map and args.map.suffix.lower() == ".pugc" else None
                (output,) = output_paths((output,), inputs=(island, args.map, sidecar, args.receipt, args.meshes))
                with lock_outputs((output,)):
                    atomic_write(output, png)
                artifacts["top_view"] = output
            if data["coverage_fraction"] < 1:
                diagnostics.append(cli.diagnostic(
                    ("terrain_uncovered" if data["coverage"] == "none" else "terrain_partial") if surface == "terrain"
                    else ("area_uncovered" if data["coverage_fraction"] == 0 else "area_partial"),
                    "Some of the rectangle has no measured surface.",
                    "Use coverage_fraction or adjust bounds, selected surfaces or Z range.", path=args.map or island,
                    field="bounds_cm"))
            result = cli.response(data, artifacts=artifacts, diagnostics=diagnostics,
                                  exit_code=cli.RESOURCE if diagnostics else 0)
        else:
            geometries = PlacementGeometry(args.meshes, args.base_level)
            data = verify_gap(args.pugc, args.anchor, args.other, args.axis, geometries,
                              expected=args.expected_cm, tolerance=args.tolerance_cm,
                              support=args.support, receipt=args.receipt)
            if args.out:
                from .ugc_storage import output_paths

                (output,) = output_paths((args.out,), inputs=(args.pugc, args.pugc.with_suffix(".pugc.json"),
                                                              args.receipt, args.meshes, args.base_level))
                save_json(output, data)
                artifacts["report"] = output
            findings = [] if data["fits"] else [cli.diagnostic("gap_mismatch", "The passage or contact is out of tolerance.",
                                                               "Check the reported gap and the support planes.")]
            result = cli.response(data, artifacts=artifacts, diagnostics=findings,
                                  exit_code=cli.FINDINGS if findings else 0)
    except ModuleNotFoundError as exc:
        if exc.name not in {"numpy", "PIL"}:
            raise
        result = cli.response({"coverage": "partial"}, diagnostics=[cli.diagnostic(
            "geometry_dependencies_missing", str(exc), "Install the toolkit with the [geometry] extra.",
        )], exit_code=cli.RESOURCE)
    except cli.INPUT_ERRORS as exc:
        result = cli.from_error(exc, path=getattr(args, "pugc", None),
                                code="invalid_map" if args.command == "measure-gap" else "invalid_input")
        if result["exit_code"] == cli.RESOURCE:
            result["data"]["coverage"] = "partial"
    if args.json:
        return result
    print(json.dumps(result if args.command in {"area", "terrain-area"} and result["diagnostics"] else
                     result["data"] or result["diagnostics"], ensure_ascii=False, indent=2))
    return result["exit_code"]
