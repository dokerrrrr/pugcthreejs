"""Collect native meshes, textures and cutout masks for selected models."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import gzip
from io import BytesIO
from itertools import islice
import json
import os
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from zipfile import ZipFile, ZipInfo

from . import cli_protocol as cli
from .resources import CATALOG, MESHES, TEXTURES
from .ugc_storage import install_staged, json_bytes, lock_outputs, output_paths

MANIFEST_ENTRY = "pugc-assets.json"
ALIASES_ENTRY = "_material_texture_aliases.json"
FLAGS_ENTRY = "_material_render_flags.json"
IMAGE_SUFFIXES = {".webp", ".png", ".jpg", ".jpeg"}


def pack_materials(archive):
    if archive is None or MANIFEST_ENTRY not in archive.NameToInfo:
        return {}
    manifest = json.loads(archive.read(MANIFEST_ENTRY))
    if manifest.get("schema") != 1:
        raise ValueError("Unsupported PUGC asset pack schema")
    return {model["objectId"]: {row["material"]: row for row in model["materials"]}
            for model in manifest["models"]}


def _resource(name, archives):
    for archive in archives:
        if name in archive.NameToInfo:
            return archive.read(name)
    return None


def _normal_path(name):
    path = re.sub(r"^/?game/", "TslGame/Content/", name.strip().replace("\\", "/"), flags=re.I)
    parent, _, leaf = path.rpartition("/")
    return (parent + "/" if parent else "") + leaf.split(".", 1)[0]


def _material_row(name, aliases, flags, bindings, candidate, saved):
    path = _normal_path(name)
    key, base = path.casefold(), path.rsplit("/", 1)[-1].casefold()
    alias = aliases.get(key) or aliases.get(base)
    if not alias and base.endswith("_wind_branch"):
        stem = base.removesuffix("_wind_branch")
        alias = aliases.get(stem + "_wind_trunk") or next(
            (value for key, value in aliases.items() if key.startswith(stem) and "trunk" in key),
            aliases.get(stem + "_wind_leaf"))
    flagged = flags.get(key) or flags.get(base) or {}
    cutoff = 0.5 if flagged.get("AlphaTest") or flagged.get("alphaTest") else saved.get("alpha_test", 0)
    override = bindings.get(name)
    if override:
        texture, binding = override, "confirmed"
    elif saved.get("texture") and (not alias or not saved.get("image_issue")):
        texture, binding = saved["texture"], saved["binding"]
    elif alias:
        texture, binding = alias + ".webp", "source_alias"
    elif candidate:
        texture, binding = candidate, "name_similarity"
    elif path.casefold().startswith("tslgame/content/"):
        texture, binding = path + ".webp", "material_path"
    else:
        texture, binding = None, "missing_binding"
    return {"material": name, "texture": texture, "binding": binding, "alpha_test": cutoff}


def _image(name, archives, masked):
    from PIL import Image

    parts = name.split("/")
    if (any(part in {"", ".", ".."} for part in parts) or "\\" in name or ":" in name
            or PurePosixPath(name).suffix.lower() not in IMAGE_SUFFIXES
            or parts[0] in {"bp-mesh", "alpha-masks"}):
        raise ValueError(f"Invalid texture entry: {name}")
    data = _resource(name, archives)
    if data is None:
        return None, None
    with Image.open(BytesIO(data)) as image:
        image.verify()
    if masked:
        with Image.open(BytesIO(data)) as image:
            if "A" in image.getbands() or "transparency" in image.info:
                mask = BytesIO()
                image.convert("RGBA").getchannel("A").save(mask, format="PNG")
                return data, mask.getvalue()
    return data, None


def _parallel(pool, operation, entries):
    entries = iter(entries)
    while batch := list(islice(entries, 16)):
        yield from zip(batch, pool.map(operation, batch), strict=True)


def collect(ids, output, *, meshes=None, textures=None, bindings=None, offline=False, inputs=()):
    from .mesh_library import parse_geometry
    from .ugc_project import load_json

    ids = tuple(ids)
    if any(type(oid) is not int or oid <= 0 for oid in ids):
        raise ValueError("Expected a list of positive integer object IDs")
    ids = sorted(set(ids))
    if output.suffix.lower() != ".zip":
        raise ValueError("Choose a ZIP output for the asset pack")
    if meshes is None and MESHES.is_file():
        meshes = MESHES
    if textures is None and TEXTURES.is_file():
        textures = TEXTURES
    for path, option in ((meshes, "--meshes"), (textures, "--textures")):
        if path is not None:
            cli.archive_input(path, option, machine=True)
    overrides = load_json(bindings) if bindings is not None else {}
    if not isinstance(overrides, dict) or not all(isinstance(path, str) and path for path in overrides.values()):
        raise ValueError("--bindings requires a JSON object mapping material names to ZIP entries")
    candidates = load_json(CATALOG / "texture_material_candidates.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    (output,) = output_paths((output,), inputs=(*inputs, meshes, textures, bindings))
    diagnostics = []
    data = {"schema": 1, "object_ids": ids, "source_revision": None,
            "models": [], "texture_count": 0, "alpha_mask_count": 0}
    with lock_outputs((output,)), ExitStack() as stack:
        archives = [stack.enter_context(ZipFile(path)) for path in dict.fromkeys(
            path.resolve() for path in (textures, meshes) if path is not None)]
        mesh_archives = list(reversed(archives))
        pool = stack.enter_context(ThreadPoolExecutor(max_workers=8))
        directory = stack.enter_context(TemporaryDirectory(prefix=".pugc-assets-", dir=output.parent))
        staged = Path(directory) / output.name
        saved = {}
        for archive in reversed(archives):
            saved.update(pack_materials(archive))

        def context(name):
            try:
                raw = _resource(name, archives)
                return {key.casefold(): value for key, value in json.loads(raw).items()} if raw else {}
            except (OSError, ValueError, RuntimeError, AttributeError) as exc:
                diagnostics.append(cli.diagnostic("material_index_unavailable", str(exc),
                                                  "Supply a valid local asset pack.", field=name))
                return {}

        def mesh(oid):
            entry = f"bp-mesh/{oid}.bin.gz"
            try:
                raw = _resource(entry, mesh_archives)
                if raw is not None:
                    geo = parse_geometry(gzip.decompress(raw), oid)
                    if not len(geo.triangles):
                        raise ValueError("Model has no triangles")
                    return entry, raw, dict.fromkeys(section.material for section in geo.sections), None
                return entry, None, {}, cli.diagnostic(
                    "mesh_missing", f"Model {oid} is unavailable.", "Check the objectId or supply a ZIP with --meshes.",
                    path=meshes, field="--meshes", object_id=oid)
            except (*cli.INPUT_ERRORS, EOFError, RuntimeError) as exc:
                return entry, None, {}, cli.diagnostic(
                    "mesh_unavailable", str(exc), "Supply a valid ZIP with --meshes.",
                    path=meshes, field=entry, object_id=oid)

        def image(name):
            try:
                texture, mask = _image(name, archives, needed[name])
                return texture, mask, None
            except (OSError, ValueError, RuntimeError) as exc:
                return None, None, str(exc)

        aliases, flags = (context(ALIASES_ENTRY), context(FLAGS_ENTRY)) if ids else ({}, {})
        needed = {}
        with ZipFile(staged, "w") as bundle:
            for oid, (entry, raw, names, issue) in _parallel(pool, mesh, ids):
                model = {"objectId": oid, "mesh": entry if raw is not None else None, "materials": []}
                data["models"].append(model)
                if issue is not None:
                    diagnostics.append(issue)
                    continue
                bundle.writestr(ZipInfo(entry), raw)
                guesses = {row["source_material"]: row["diffuse_guess"] for row in candidates.get(str(oid), ())}
                for name in names:
                    if not name.strip() or name.strip().casefold() == "none":
                        continue
                    row = _material_row(name, aliases, flags, overrides, guesses.get(name),
                                        saved.get(oid, {}).get(name, {}))
                    model["materials"].append(row)
                    if texture := row["texture"]:
                        needed[texture] = needed.get(texture, False) or bool(row["alpha_test"])
            images = {}
            for name, (texture, mask, issue) in _parallel(pool, image, needed):
                mask_path = None
                if texture is not None:
                    bundle.writestr(ZipInfo(name), texture)
                    data["texture_count"] += 1
                    if mask is not None:
                        mask_path = f"alpha-masks/{name}.png"
                        bundle.writestr(ZipInfo(mask_path), mask)
                        data["alpha_mask_count"] += 1
                images[name] = texture is not None, mask_path, issue
            for model in data["models"]:
                for row in model["materials"]:
                    available, mask, issue = images.get(row["texture"], (False, None, None))
                    if not available:
                        row["image_issue"] = "texture_missing" if row["texture"] else "missing_binding"
                        diagnostics.append(cli.diagnostic(
                            row["image_issue"], issue or f"No texture for {row['material']}.",
                            "Pass a local texture ZIP and a mapping with --bindings.",
                            path=textures, field=row["texture"] or row["material"], object_id=model["objectId"]))
                    elif row["alpha_test"]:
                        if mask is not None:
                            row["alpha_mask"] = mask
                        else:
                            row["mask_issue"] = "alpha_mask_missing"
                            diagnostics.append(cli.diagnostic(
                                row["mask_issue"], f"The cutout texture {row['texture']} has no alpha channel.",
                                "Supply a texture with the alpha channel for this material.", path=textures,
                                field=row["material"], object_id=model["objectId"]))
            data["coverage"] = "partial" if diagnostics else "complete"
            bundle.writestr(ZipInfo(MANIFEST_ENTRY), json_bytes(data))
        artifacts = {}
        if not diagnostics or (not output.exists() and any(row["mesh"] for row in data["models"])):
            with staged.open("r+b") as stream:
                os.fsync(stream.fileno())
            install_staged(staged, output)
            artifacts["archive"] = output
    return cli.response(data, artifacts=artifacts, diagnostics=diagnostics,
                        exit_code=cli.RESOURCE if diagnostics else 0)


def add_arguments(parser):
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--project", type=Path, help="Project JSON or a written PUGC map")
    selection.add_argument("--ids", type=int, nargs="+", metavar="ID", help="One or more object IDs")
    selection.add_argument("--ids-file", type=Path, help="JSON array or whitespace/comma separated text file")
    parser.add_argument("--out", type=Path, required=True, help="Output ZIP for measurements and rendering")
    parser.add_argument("--meshes", type=Path, help="Read meshes from this local ZIP")
    parser.add_argument("--textures", type=Path, help="Read textures from this local ZIP")
    parser.add_argument("--bindings", type=Path, help="JSON overrides for material texture paths")
    parser.add_argument("--offline", action="store_true", help="Compatibility flag. Asset inputs are always local.")


def run(args):
    try:
        if args.project is not None:
            from .inspect_project import read_project
            from .pugc_codec import read_game

            records = (read_game(args.project)["objects"] if args.project.suffix.lower() == ".pugc" else
                       read_project(args.project)[0]["instances"])
            ids = [record["objectId"] for record in records]
        elif args.ids_file is not None:
            text = args.ids_file.read_text(encoding="utf-8-sig")
            ids = (json.loads(text) if args.ids_file.suffix.lower() == ".json" or text.lstrip().startswith("[") else
                   [int(word) for word in re.split(r"[\s,]+", text.strip()) if word])
            if not isinstance(ids, list):
                raise ValueError("Expected a JSON array of object IDs")
        else:
            ids = args.ids
        result = collect(ids, args.out, meshes=args.meshes, textures=args.textures, bindings=args.bindings,
                         offline=args.offline, inputs=(args.project, args.ids_file))
    except ModuleNotFoundError as exc:
        if exc.name not in {"numpy", "PIL"}:
            raise
        result = cli.response(diagnostics=[cli.diagnostic("geometry_dependencies_missing", str(exc),
                              "Install the toolkit with the [geometry] extra.")], exit_code=cli.RESOURCE)
    except cli.INPUT_ERRORS as exc:
        result = cli.from_error(exc, path=args.project or args.ids_file)
    if args.json:
        return result
    print(json.dumps(result if result["diagnostics"] else result["data"], ensure_ascii=False, indent=2))
    return result["exit_code"]
