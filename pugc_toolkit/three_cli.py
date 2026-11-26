"""CLI entry points for headless rendering and geometry tools."""
import json
from pathlib import Path

from . import cli_protocol as cli
from .base_level import add_arguments as base_arguments, resolve_base_level
from .resources import CATALOG, MESHES, PROFILE
from .web_service import STATIC


def add_scene_arguments(parser):
    base_arguments(parser)
    parser.add_argument("--offline", action="store_true", help="Use only the cached or supplied island")
    parser.add_argument("--meshes", type=Path, default=MESHES)
    parser.add_argument("--profile", type=Path, default=PROFILE)
    parser.add_argument("--textures", type=Path)
    parser.add_argument("--bindings", type=Path)


def add_commands(commands):
    preview = commands.add_parser("preview", help="Render a written PUGC map in Three.js")
    flight = commands.add_parser("flythrough", help="Render a timed camera path to MP4")
    flight.add_argument("--path", type=Path, required=True, help="JSON camera keyframes")
    flight.add_argument("--ffmpeg", type=Path, help="FFmpeg executable with libx264")
    compare = commands.add_parser("compare", help="Compare a written map with reference cameras")
    compare.add_argument("source", type=Path)
    compare.add_argument("--cameras", nargs="+")
    compare.add_argument("--strict", action="store_true")
    camera = preview.add_mutually_exclusive_group()
    camera.add_argument("--camera", help="ID or name of a reference camera")
    camera.add_argument("--camera-file", type=Path, help="JSON camera pose in game coordinates")
    camera.add_argument("--eye-cm", type=float, nargs=3, metavar=("X", "Y", "Z"), help="Perspective camera position in game coordinates")
    preview.add_argument("--target-cm", type=float, nargs=3, metavar=("X", "Y", "Z"), help="Aim the perspective camera at this game position")
    preview.add_argument("--fov-deg", type=float, help="Vertical field of view for --eye-cm (default 50 degrees)")
    preview.add_argument("--roll-deg", type=float, help="Camera roll around its local Z axis")
    preview.add_argument("--clip-cm", type=float, nargs=2, metavar=("NEAR", "FAR"))
    preview.add_argument("--ortho-height-cm", type=float, help="Orthographic field height in centimetres")
    preview.add_argument("--reference", type=Path)
    preview.add_argument("--view", choices=("top", "side", "iso"), help="Preset view (default iso)")
    for shot in (preview, flight):
        shot.add_argument("--size", nargs=2, type=int, default=(1280, 720), metavar=("WIDTH", "HEIGHT"))
    frame = preview.add_mutually_exclusive_group()
    frame.add_argument("--ids", nargs="+")
    frame.add_argument("--frame", choices=("objects", "all"), default="objects")
    for sub in (preview, compare, flight):
        sub.add_argument("pugc", type=Path)
        sub.add_argument("--receipt", type=Path)
        sub.add_argument("--no-island", action="store_true")
        sub.add_argument("--mode", choices=("geometry", "textured"), default="geometry")
        sub.add_argument("--chromium", type=Path)
        sub.add_argument("--out", type=Path, required=True)
        add_scene_arguments(sub)
    for sub in (preview, compare):
        sub.add_argument("--project", type=Path, help="Project containing reference annotations")
        sub.add_argument("--scene", help="Scene name inside the reference archive")

    audit = commands.add_parser("audit", help="Audit a written map with CPU geometry")
    audit.add_argument("pugc", type=Path)
    audit.add_argument("--project", required=True, type=Path)
    audit.add_argument("--meshes", type=Path, default=MESHES)
    audit.add_argument("--profile", type=Path, default=PROFILE)
    audit.add_argument("--receipt", type=Path)
    audit.add_argument("--origin-x-cm", type=float)
    audit.add_argument("--out", required=True, type=Path)
    audit.add_argument("--strict", action="store_true")
    audit.add_argument("--require", nargs="+", choices=("road_joins", "viewpoints", "approved_road"), default=())


def prepare(args, island):
    from .scene import map_scene, texture_scene
    from .ugc_project import load_json

    result = map_scene(args.pugc, args.meshes, receipt_path=args.receipt, profile=args.profile, island=island)
    if args.mode == "textured":
        bindings = load_json(args.bindings) if args.bindings else {}
        texture_scene(result, args.textures, load_json(CATALOG / "texture_material_candidates.json"), bindings,
                      meshes=args.meshes)
    return result


def missing_meshes(scene, path):
    return [cli.missing_mesh(path, instance_id=row["instance"], object_id=row["objectId"])
            for row in scene.manifest["missing_meshes"]]


def preview(args, scene, project, origin):
    from .camera_motion import FIELDS, camera_record
    from .rendering import renderer
    from .scene import reference_scene
    from .ugc_project import load_json
    from .ugc_storage import output_paths

    camera = None
    pose = {}
    preset = args.view or "iso"
    width, height = args.size
    if not 1 <= width <= 4096 or not 1 <= height <= 4096 or args.out.suffix.lower() != ".png":
        raise ValueError("Choose a PNG output and dimensions from 1 to 4096")
    settings = {field: getattr(args, field) for field in FIELDS if getattr(args, field) is not None}
    if args.camera_file is not None and settings:
        raise ValueError("Put all manual camera settings in --camera-file or pass the camera flags")
    if settings or args.camera_file is not None:
        if args.ids or args.frame == "all" or args.view is not None:
            raise ValueError("--view, --ids and --frame all require a preset camera")
        camera, info = camera_record(load_json(args.camera_file) if args.camera_file else settings, args.size)
        pose = {"camera_pose": info}
    if args.camera:
        if not args.reference:
            raise ValueError("--camera requires --reference")
        ref = reference_scene(args.reference, project, args.scene, origin=origin)
        matches = [item for item in ref.manifest["cameras"] if args.camera in (item["id"], item["name"])]
        if len(matches) != 1:
            raise cli.CliError(cli.RESOURCE, "camera_unavailable", f"Camera {args.camera} is missing or ambiguous.",
                               "Choose an available camera ID.")
        camera = matches[0]
    invalid_ids = set(args.ids or ()) - {row["id"] for row in scene.manifest["instances"]}
    if invalid_ids:
        raise ValueError(f"Unknown placement IDs: {sorted(invalid_ids)}")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    (output,) = output_paths((args.out,), inputs=inputs(args))
    with renderer(scene, chromium=args.chromium) as (render, (url,)):
        metrics = render.frame(url, output, camera=camera, resolution=(width, height), preset=preset,
                               ids=args.ids, island=args.frame == "all")
    diagnostics = missing_meshes(scene, args.meshes)
    return cli.response({"pugc_sha256": scene.manifest["pugc_sha256"], "project_sha256": scene.manifest["project_sha256"],
                         "camera": args.camera or ("manual" if pose else preset), "resolution": [width, height],
                         **pose,
                         "coverage": "partial" if diagnostics else "complete", "render": metrics},
                        artifacts={"frame": output}, diagnostics=diagnostics,
                        exit_code=cli.RESOURCE if diagnostics else 0)


def inputs(args):
    return tuple(getattr(args, field, None) for field in
                 ("pugc", "receipt", "project", "profile", "meshes", "base_level", "bindings", "textures",
                  "reference", "source", "camera_file")) + (args.pugc.with_suffix(args.pugc.suffix + ".json"),)


def comparison(args, result, project, origin, island):
    from .geometry_report import completion, partial_diagnostics
    from .rendering import renderer
    from .scene import reference_scene
    from .three_compare import compare
    from .ugc_project import save_json
    from .ugc_storage import output_paths

    source = reference_scene(args.source, project, args.scene, island, origin)
    report, cameras = compare(source, result, project, args.cameras, args.strict)
    targets = [args.out / "comparison.json"]
    targets.extend(args.out / frame[side] for frame in report["frames"] for side in ("source", "result"))
    args.out.mkdir(parents=True, exist_ok=True)
    targets = output_paths(targets, inputs=inputs(args))
    report_path, *frames = targets
    if frames:
        with renderer(source, result, chromium=args.chromium) as (render, urls):
            for frame in report["frames"]:
                for side, url in zip(("source", "result"), urls, strict=True):
                    frame[f"{side}_render"] = render.frame(url, args.out / frame[side],
                                                           camera=cameras[frame["camera_id"]],
                                                           resolution=frame["resolution"])
    save_json(report_path, report)
    diagnostics = missing_meshes(result, args.meshes)
    for index, finding in enumerate(report["issues"]):
        diagnostics.append(cli.diagnostic("geometry_mismatch",
                           f"{finding['issue']}: {finding['landmark']} / {finding['camera']}",
                           "Inspect the paired frames and correct the placement.", path=report_path,
                           field=f"issues[{index}]", instance_id=finding["instance_ids"][0]))
    diagnostics.extend(partial_diagnostics(report["geometry_check"], report_path))
    return cli.response({"pugc_sha256": report["pugc_sha256"], "comparison_key": report["comparison_key"],
                         "geometry_check": report["geometry_check"],
                         "issues": [{key: row[key] for key in ("landmark", "camera", "issue", "instance_ids")}
                                    for row in report["issues"]]},
                        artifacts={"report": report_path, "source_frames": frames[::2], "result_frames": frames[1::2]},
                        diagnostics=diagnostics, exit_code=completion(report["geometry_check"]))


def run(args, parser):
    try:
        if args.command == "audit":
            from .audit_route import run as audit

            machine = parser if args.json else cli.MachineParser()
            result = audit(args, machine, machine=True)
        else:
            cli.archive_input(args.meshes, "--meshes", machine=True)
            if args.textures:
                cli.archive_input(args.textures, "--textures", machine=True)
            island = None if getattr(args, "no_island", False) else resolve_base_level(args, cli.MachineParser())
            from .project_migration import annotation_origin
            from .ugc_project import load_json

            scene = prepare(args, island)
            if args.command == "flythrough":
                from .camera_motion import render_flight

                data, output = render_flight(args, scene, inputs=(*inputs(args), island))
                diagnostics = missing_meshes(scene, args.meshes)
                data.update(pugc_sha256=scene.manifest["pugc_sha256"], project_sha256=scene.manifest["project_sha256"],
                            coverage="partial" if diagnostics else "complete")
                result = cli.response(data, artifacts={"video": output}, diagnostics=diagnostics,
                                      exit_code=cli.RESOURCE if diagnostics else 0)
            else:
                project = load_json(args.project) if args.project else {}
                origin = annotation_origin(project, scene.manifest["annotation_origin_x_cm"])
                result = (preview(args, scene, project, origin) if args.command == "preview" else
                          comparison(args, scene, project, origin, island))
    except ModuleNotFoundError as exc:
        if exc.name not in {"numpy", "playwright", "trimesh"}:
            raise
        result = cli.response({"coverage": "partial"}, diagnostics=[cli.diagnostic(
            f"{exc.name}_missing", str(exc), "Install the toolkit with the [render] extra."
        )], exit_code=cli.RESOURCE)
    except cli.INPUT_ERRORS as exc:
        result = cli.from_error(exc, path=getattr(args, "pugc", None), code="invalid_map")
        if result["exit_code"] == cli.RESOURCE:
            result["data"]["coverage"] = "partial"
    if args.json:
        return result
    print(json.dumps(result["diagnostics"] or result["data"], ensure_ascii=False, indent=2))
    return result["exit_code"]
