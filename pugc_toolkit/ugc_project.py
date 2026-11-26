"""Edit a UGC project, search and inspect assets, and build PUGC v3."""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
from importlib.util import find_spec
import json
from math import cos, isfinite, radians, sin
from pathlib import Path
import shutil
import sys
import time
import traceback

from . import __version__
from . import cli_protocol as cli
from .catalog_assets import asset_card, coverage, search_assets, search_catalog
from .game_space import PROJECT_SCHEMA, SPACE, TRANSFORM, is_native, native_records, transform_key
from .legacy_layout import legacy_transform, rebase_legacy_transform
from .pugc_codec import (CONTAINER_VERSION, LAYOUT_VERSION, PugcError, convert_layout,
                         decode_raw, encode_raw, read_game, read_pugc_bytes)
from .resources import CATALOG, MESHES, PROFILE, ROOT, TEMPLATE, TEXTURES
from .ugc_storage import (PublicationError, atomic_write, json_bytes, lock_outputs, output_paths,
                         publish_outputs, recover_outputs, require_no_transaction)
from .ugc_validation import (HOUSE_SCALE_WARNING, ZONES, finite as _finite_number, game_header_diagnostics,
                             project_errors, scaled_building, validate)

DEFAULT_ORIGIN_X_CM = 50000


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf8"))


def save_json(path, value):
    atomic_write(path, json_bytes(value))


def xyz(vector):
    return [vector[k] for k in "xyz"]


def vector(values):
    return dict(zip("xyz", values))


def transform(position_m, yaw=0, scale=(1, 1, 1)):
    angle = radians(yaw) / 2
    return {"translation": vector(n * 100 for n in position_m),
            "rotation": dict(x=0, y=0, z=sin(angle), w=cos(angle)), "scale3D": vector(scale)}


def project_from_map(raw, origin_x_cm=DEFAULT_ORIGIN_X_CM, *, receipt=None):
    authored = convert_layout(raw, origin_x_cm)
    objects = authored.pop("objects")
    project = {"schema": 1, "layout_version": LAYOUT_VERSION, "origin_x_cm": origin_x_cm,
               "game": authored, "instances": []}
    if receipt is not None and "approved_road" in receipt:
        project["approved_road"] = _current_approvals(
            receipt["approved_road"], receipt["instances"], raw["objects"], origin_x_cm)
    approvals = project.get("approved_road", {})
    for index, (entry, game_entry) in enumerate(zip(objects, raw["objects"], strict=True)):
        metadata = ({"id": f"import-{index:04d}", "objectId": entry["objectId"],
                     "assembly": "Imported", "zone": "near"} if receipt is None
                    else deepcopy(receipt["instances"][index]))
        approval = approvals.get(metadata["id"])
        project["instances"].append({
            **metadata,
            "transform": deepcopy(approval["transform"]) if approval else entry["spawnTransform"],
            "game_original": deepcopy(game_entry),
        })
    return project


def _read_build_version(source):
    game = read_game(source)
    data_version = game.get("dataVersion")
    if type(data_version) is not int or data_version != 3:
        raise PugcError("Expected a version source with dataVersion 3")
    build_version = game.get("buildVersion")
    if not isinstance(build_version, str) or not build_version.strip():
        raise PugcError("Expected a nonempty buildVersion in the version source")
    return build_version


def new_project(name, template=None, *, version_from=None):
    if template is not None and version_from is not None:
        raise ValueError("Choose either template or version_from")
    if template is None:
        game = load_json(TEMPLATE)
        if version_from is not None:
            game["buildVersion"] = _read_build_version(version_from)
        now = int(time.time())
        game["editFileData"].update(createTime=now, lastModifiedTime=now)
    else:
        game = read_game(template)
        version = game.get("dataVersion") if isinstance(game, dict) else None
        if type(version) is not int or version != 3:
            raise ValueError("Expected a PUGC v3 template with dataVersion 3")

    game["name"] = name
    game["creatorData"] = {**(game.get("creatorData") or {}), "name": name, "description": ""}
    game.pop("objects", None)
    game.pop("modObjects", None)
    game["deviceEventList"] = []
    for field in ("deviceConnections", "deviceEventLinks"):
        if field in game:
            game[field] = []
    return {"schema": 1, "layout_version": LAYOUT_VERSION, "origin_x_cm": DEFAULT_ORIGIN_X_CM,
            "game": game, "instances": []}


def new_game_project(name, template=None, *, version_from=None):
    legacy = new_project(name, template, version_from=version_from)
    return {"schema": PROJECT_SCHEMA, "coordinate_space": SPACE,
            "game": legacy["game"], "instances": []}


def native_project_from_map(raw, receipt=None):
    game = {key: deepcopy(value) for key, value in raw.items() if key != "objects"}
    project = {"schema": PROJECT_SCHEMA, "coordinate_space": SPACE, "game": game, "instances": []}
    objects = raw["objects"]
    metadata = receipt["instances"] if receipt else [
        {"id": f"import-{i:04d}", "objectId": row["objectId"], "assembly": "Imported", "zone": "near"}
        for i, row in enumerate(objects)
    ]
    approved = (receipt or {}).get("approved_road", {})
    for entry, record in zip(metadata, objects, strict=True):
        project["instances"].append({**deepcopy(entry), TRANSFORM: deepcopy(record[TRANSFORM]),
                                     "game_original": deepcopy(record)})
    if approved:
        project["approved_road"] = {
            entry["id"]: {"objectId": entry["objectId"], TRANSFORM: deepcopy(entry[TRANSFORM])}
            for entry in project["instances"] if entry["id"] in approved
        }
    return project


def instance(project, instance_id):
    return next((entry for entry in project["instances"] if entry["id"] == instance_id), None)


def place(project, instance_id, object_id, placement, assembly, zone, source=None, role=None):
    if not instance_id or instance(project, instance_id) is not None:
        raise ValueError(f"Invalid or duplicate instance ID {instance_id!r}")
    if not assembly or zone not in ZONES:
        raise ValueError("Placement requires a named assembly and a valid zone")
    entry = {"id": instance_id, "objectId": object_id, transform_key(project): placement,
             "assembly": assembly, "zone": zone}
    if source:
        entry["source"] = source
    if role:
        entry["role"] = role
    project["instances"].append(entry)
    return entry


def game_records(project):
    errors = project_errors(project)
    if errors:
        raise ValueError("\n".join(errors))
    return _game_records(project)


def _game_records(project):
    if is_native(project):
        return native_records(project)
    origin = project["origin_x_cm"]
    legacy = "layout_version" not in project
    raw = []
    for entry in project["instances"]:
        oid, t = entry["objectId"], entry["transform"]
        old = entry.get("game_original")
        obj = deepcopy(old) if old is not None else {"deviceIndex": -1, "userDeviceName": ""}
        previous = None
        if old is not None:
            previous = _layout_transform(old, origin)
            if legacy:
                t = rebase_legacy_transform(t, old, previous, origin)
            if previous == t and old["objectId"] == oid:
                raw.append(obj)
                continue
        obj.update(objectId=oid, spawnTransform=t)
        record = convert_layout({"objects": [obj]}, origin)["objects"][0]
        if previous is not None and previous["translation"]["x"] == t["translation"]["x"]:
            record["spawnTransform"]["translation"]["x"] = old["spawnTransform"]["translation"]["x"]
        raw.append(record)
    return {**project["game"], "objects": raw}


def check_project_output(project, profile, allowed_ids=None):
    """Check structure before coordinate conversion, then validate the exact output records."""
    errors = project_errors(project)
    if not errors:
        entries = project["instances"]
        originals = [entry.get("game_original") for entry in entries]
        errors, _ = game_header_diagnostics(project["game"], profile, imported=any(originals))
    if errors:
        return None, {"valid": False, "errors": errors, "warnings": [], "budget": [],
                      "profile": profile["id"], "profile_revision": profile["revision"]}

    payload = _game_records(project)
    labels = [f"instances[{i}] ({entry['id']!r})" for i, entry in enumerate(entries)]
    report = validate(payload, profile, [entry["zone"] for entry in entries], allowed_ids,
                      originals=originals, labels=labels)
    return payload, report


class BuildValidationError(ValueError):
    def __init__(self, report):
        super().__init__("\n".join(report["errors"]))
        self.report = report


def _sidecar_path(binary):
    binary = Path(binary)
    return binary.with_suffix(binary.suffix + ".json")


def _map_outputs(binary, raw_json=None, *, inputs=()):
    outputs = [binary, _sidecar_path(binary)]
    if raw_json is not None:
        outputs.append(raw_json)
    return output_paths(outputs, inputs=inputs)


def _import_sources(project):
    provenance = project.get("import_source") if isinstance(project, dict) else None
    if isinstance(provenance, dict):
        return tuple(path for key in ("pugc", "receipt") if isinstance(path := provenance.get(key), str) and path)
    return ()


def _checked_build(project, paths, profile, allowed_ids, *, build_version=None):
    with lock_outputs(paths):
        require_no_transaction(paths[0])
        payload, report = check_project_output(project, profile, allowed_ids)
        if not report["valid"]:
            raise BuildValidationError(report)
        if build_version is not None:
            payload["buildVersion"] = build_version
            report["buildVersion"] = build_version
        return _write_build(project, payload, paths), report


def build(project, output, raw_json=None, *, profile=None, allowed_ids=None, version_from=None):
    paths = _map_outputs(output, raw_json, inputs=(version_from, *_import_sources(project)))
    profile = load_json(PROFILE) if profile is None else profile
    build_version = _read_build_version(version_from) if version_from is not None else None
    return _checked_build(project, paths, profile, allowed_ids, build_version=build_version)[0]


def _write_build(project, payload, paths):
    encoded = encode_raw(payload)
    excluded = {"transform", TRANSFORM, "game_original"}
    receipt = {
        "schema": project["schema"], "pugc_sha256": hashlib.sha256(encoded).hexdigest(),
        "instances": [{k: v for k, v in entry.items() if k not in excluded}
                      for entry in project["instances"]],
    }
    if is_native(project):
        receipt.update(coordinate_space=SPACE, approved_road=deepcopy(project.get("approved_road", {})))
    else:
        origin = project["origin_x_cm"]
        receipt.update(layout_version=LAYOUT_VERSION, origin_x_cm=origin, approved_road=_current_approvals(
            project.get("approved_road", {}), project["instances"], payload["objects"], origin))
    contents = [encoded, json_bytes(receipt)]
    if len(paths) == 3:
        contents.append(json_bytes(payload))

    def verify(staged):
        data = read_pugc_bytes(staged[0])
        reopened = decode_raw(data)
        metadata = load_json(staged[1])
        _verify_receipt(metadata, reopened, hashlib.sha256(data).hexdigest(), project.get("origin_x_cm"))
        if reopened != payload or metadata != receipt or (len(staged) == 3 and load_json(staged[2]) != payload):
            raise PublicationError("Staged output does not match the build")

    publish_outputs(paths, contents, verify)
    return receipt


def _valid_transform(value):
    if not isinstance(value, dict):
        return False
    for field, axes in (("translation", "xyz"), ("rotation", "xyzw"), ("scale3D", "xyz")):
        part = value.get(field)
        if not isinstance(part, dict) or not all(_finite_number(part.get(axis)) for axis in axes):
            return False
    rotation = value["rotation"]
    norm_squared = sum(float(rotation[axis]) * rotation[axis] for axis in "xyzw")
    return isfinite(norm_squared) and norm_squared > 0


def _layout_transform(entry, origin):
    return convert_layout({"objects": [entry]}, origin)["objects"][0]["spawnTransform"]


def _approval_matches_map(snapshot, game_entry, origin, convert=_layout_transform):
    if snapshot == convert(game_entry, origin):
        return True
    # The writer rounds translations during conversion. Compare in both directions so a
    # matching approval can retain its original authoring precision.
    approved_entry = {**game_entry, "spawnTransform": snapshot}
    return convert(approved_entry, origin) == game_entry["spawnTransform"]


def _current_approvals(approvals, instances, objects, origin):
    """Keep exact approval precision and update historical snapshots to the current basis."""
    result = deepcopy(approvals)
    for metadata, obj in zip(instances, objects, strict=True):
        approval = result.get(metadata["id"])
        if approval and not _approval_matches_map(approval["transform"], obj, origin):
            approval["transform"] = _layout_transform(obj, origin)
    return result


def _verify_receipt(receipt, raw, digest, origin_override):
    if (not isinstance(receipt, dict) or type(receipt.get("schema")) is not int
            or receipt["schema"] not in (1, PROJECT_SCHEMA)):
        raise ValueError("unsupported receipt schema (expected 1 or 2)")
    native = is_native(receipt)
    if native and (receipt.get("coordinate_space") != SPACE or {"origin_x_cm", "layout_version"} & receipt.keys()):
        raise ValueError("schema 2 receipt must use game-xyz-cm")
    if "layout_version" in receipt and (type(receipt["layout_version"]) is not int
                                        or receipt["layout_version"] != LAYOUT_VERSION):
        raise ValueError(f"unsupported layout_version (expected {LAYOUT_VERSION})")
    if receipt.get("pugc_sha256") != digest:
        raise ValueError("SHA-256 does not match the PUGC file")
    origin = receipt.get("origin_x_cm")
    if not native and not _finite_number(origin):
        raise ValueError("origin_x_cm must be a finite number")
    if not native and origin_override is not None and origin_override != origin:
        raise ValueError("origin override does not match the receipt")

    instances, objects = receipt.get("instances"), raw.get("objects")
    if not isinstance(instances, list) or not isinstance(objects, list) or len(instances) != len(objects):
        raise ValueError("instances must match the object count in the PUGC file")
    by_id = {}
    for index, (meta, obj) in enumerate(zip(instances, objects)):
        label = f"instances[{index}]"
        if (not isinstance(meta, dict) or not isinstance(obj, dict)
                or type(meta.get("objectId")) is not int or type(obj.get("objectId")) is not int
                or meta["objectId"] != obj["objectId"]):
            raise ValueError(f"{label}.objectId must match the PUGC object at the same index")
        key, assembly = meta.get("id"), meta.get("assembly")
        if not isinstance(key, str) or not key or key in by_id:
            raise ValueError(f"{label}.id must be a nonempty, unique string")
        if not isinstance(assembly, str) or not assembly or meta.get("zone") not in ZONES:
            raise ValueError(f"{label} must have a nonempty assembly and a supported zone")
        if {"transform", TRANSFORM, "game_original"} & meta.keys():
            raise ValueError(f"{label} contains fields reserved for the project")
        by_id[key] = (meta, obj)

    approved = receipt.get("approved_road", {})
    if not isinstance(approved, dict):
        raise ValueError("approved_road must be an object")
    field = transform_key(receipt)
    for key, approval in approved.items():
        meta, obj = by_id.get(key, ({}, {}))
        if (meta.get("zone") != "road" or not isinstance(approval, dict)
                or approval.keys() != {"objectId", field}
                or type(approval["objectId"]) is not int or approval["objectId"] != meta["objectId"]
                or not _valid_transform(approval[field]) or not _valid_transform(obj.get(TRANSFORM))):
            raise ValueError(f"approved_road[{key!r}] does not match the PUGC road instance")
        matches = (approval[field] == obj[TRANSFORM] if native else
                   _approval_matches_map(approval[field], obj, origin) or
                   ("layout_version" not in receipt and _approval_matches_map(approval[field], obj, origin, legacy_transform)))
        if not matches:
            raise ValueError(f"approved_road[{key!r}] does not match the PUGC road instance")


def raw_with_receipt(path, receipt_path=None, origin_x_cm=None, *, ignore_receipt=False):
    path = Path(path)
    require_no_transaction(path)
    if ignore_receipt and receipt_path is not None:
        raise ValueError("Cannot use an explicit receipt when ignoring receipts")
    if origin_x_cm is not None and not _finite_number(origin_x_cm):
        raise ValueError("Expected a finite origin_x_cm")

    data = read_pugc_bytes(path)
    digest = hashlib.sha256(data).hexdigest()
    raw = decode_raw(data)
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: PUGC payload must be a JSON object")

    sidecar = Path(receipt_path) if receipt_path is not None else _sidecar_path(path)
    if not ignore_receipt and (receipt_path is not None or sidecar.exists() or sidecar.is_symlink()):
        try:
            receipt = load_json(sidecar)
            _verify_receipt(receipt, raw, digest, origin_x_cm)
        except ValueError as exc:
            raise ValueError(f"Receipt {sidecar}: {exc}") from exc
        require_no_transaction(path)
        return raw, receipt

    entries = raw.get("objects")
    receipt = {"pugc_sha256": digest, "origin_x_cm": DEFAULT_ORIGIN_X_CM if origin_x_cm is None else origin_x_cm,
               "instances": [
                   {"id": f"objects[{i}]", "objectId": item.get("objectId") if isinstance(item, dict) else None}
                   for i, item in enumerate(entries if isinstance(entries, list) else [])
               ]}
    require_no_transaction(path)
    if not ignore_receipt and (sidecar.exists() or sidecar.is_symlink()):
        raise PublicationError(f"Companion JSON appeared while reading {path}; retry the read")
    return raw, receipt


def read_with_receipt(path, receipt_path=None, origin_x_cm=None):
    raw, receipt = raw_with_receipt(path, receipt_path, origin_x_cm)
    return convert_layout(raw, receipt.get("origin_x_cm", DEFAULT_ORIGIN_X_CM)), receipt


def rotate_assembly(project, name, center_m, yaw):
    batch = [entry for entry in project["instances"] if entry["assembly"] == name]
    if not batch:
        raise ValueError(f"Unknown assembly {name!r}")
    if any(entry["id"] in project.get("approved_road", {}) for entry in batch):
        raise ValueError("Assembly contains approved road placements")
    x0, y0, _ = center_m if is_native(project) else (v * 100 for v in center_m)
    angle = radians(yaw)
    c, s = cos(angle), sin(angle)
    half_c, half_s = cos(angle / 2), sin(angle / 2)
    for entry in batch:
        t = entry[transform_key(project)]
        x, y, z = xyz(t["translation"])
        dx, dy = x - x0, y - y0
        t["translation"] = vector((x0 + c * dx - s * dy, y0 + s * dx + c * dy, z))
        q = t["rotation"]
        t["rotation"] = dict(x=half_c*q["x"] - half_s*q["y"], y=half_s*q["x"] + half_c*q["y"],
                             z=half_c*q["z"] + half_s*q["w"], w=half_c*q["w"] - half_s*q["z"])


def approve_road(project):
    project["approved_road"] = {
        entry["id"]: deepcopy({"objectId": entry["objectId"], transform_key(project): entry[transform_key(project)]})
        for entry in project["instances"] if entry["zone"] == "road"
    }


def export_obj(geometry, output):
    with Path(output).open("w", encoding="utf8") as stream:
        for x, y, z in geometry.vertices:
            stream.write(f"v {x:.8g} {y:.8g} {z:.8g}\n")
        for a, b, c in geometry.triangles:
            stream.write(f"f {a+1} {b+1} {c+1}\n")


def _parser(prog, machine):
    from . import asset_bundle, asset_packs, batch_edit, blender_cli, inspect_project, measurement_cli, three_cli, validate_pugc

    parser_type = cli.MachineParser if machine else argparse.ArgumentParser
    parser = parser_type(prog=prog, description=__doc__, allow_abbrev=False)
    if machine:
        parser.add_argument("--version", action="store_true", help="Show the tool version")
    else:
        parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--json", action="store_true", help="Emit one versioned JSON response (place before the command)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities", help="List versions, commands and optional executables")
    asset_packs.add_doctor_arguments(commands.add_parser("doctor", help="Inspect asset packs without downloading"))
    asset_packs.add_fetch_arguments(commands.add_parser("fetch-pack", help="Install a verified asset pack"))
    asset_bundle.add_arguments(commands.add_parser("fetch-assets", help="Collect meshes and textures by object ID"))

    def action(name, *positionals):
        sub = commands.add_parser(name, help=COMMAND_HELP[name])
        for key in positionals:
            sub.add_argument(key)
        return sub

    init = action("init", "project")
    init.add_argument("--name", required=True)
    init.add_argument("--legacy-layout", action="store_true", help="Use the Toolkit 1.0.1 authoring format")
    init_source = init.add_mutually_exclusive_group()
    init_source.add_argument("--template", type=Path, help="Use settings and metadata from a PUGC v3 file")
    init_source.add_argument("--version-from", type=Path, metavar="MAP",
                             help="Use only the buildVersion from a PUGC v3 file")
    load = action("import", "pugc", "project")
    receipts = load.add_mutually_exclusive_group()
    receipts.add_argument("--receipt", type=Path, help="Use this companion JSON instead of the adjacent file")
    receipts.add_argument("--ignore-receipt", action="store_true", help="Import only the binary map")
    load.add_argument("--legacy-layout", action="store_true", help="Import into the Toolkit 1.0.1 authoring format")
    load.add_argument("--origin-x-cm", type=float, help="Use this origin when importing a map without its receipt")
    write = action("build", "project", "pugc")
    write.add_argument("--raw-json")
    write.add_argument("--version-from", type=Path, metavar="MAP",
                       help="Set the output buildVersion from a PUGC v3 file")
    write.add_argument("--profile", default=PROFILE)
    write.add_argument("--allowed-ids", nargs="*", type=int)
    recover = action("recover", "pugc")
    recover.add_argument("--raw-json", help="Use the same raw JSON path as the interrupted build")
    action("game-json", "project", "output")
    action("migrate", "project", "output")
    add = action("place", "project", "id")
    add.add_argument("objectId", type=int)
    position = add.add_mutually_exclusive_group(required=True)
    position.add_argument("--at-cm", type=float, nargs=3, metavar=("X", "Y", "Z"),
                          help="Game coordinates in centimetres")
    position.add_argument("--at", type=float, nargs=3, help="Centimetres for schema 2; authoring metres for schema 1")
    position.add_argument("--at-m", type=float, nargs=3, dest="at_m", metavar=("X", "Y", "Z"),
                          help="Legacy authoring coordinates in metres")
    add.add_argument("--yaw-deg", "--yaw", type=float, default=0, help="Rotation about Z in degrees")
    add.add_argument("--scale", type=float, nargs=3, default=(1, 1, 1), metavar=("X", "Y", "Z"),
                     help="Scale factors")
    add.add_argument("--assembly", required=True)
    add.add_argument("--zone", choices=ZONES, default="near")
    add.add_argument("--source")
    add.add_argument("--role")
    group = action("group", "project", "assembly")
    group.add_argument("ids", nargs="+")
    rotate = action("rotate", "project", "assembly")
    center = rotate.add_mutually_exclusive_group(required=True)
    center.add_argument("--center-cm", type=float, nargs=3, help="Pivot in game centimetres")
    center.add_argument("--center", type=float, nargs=3, help="Legacy pivot in authoring metres")
    rotate.add_argument("--yaw", type=float, required=True, help="Rotation about Z in degrees")
    action("approve-road", "project")
    batch_edit.add_arguments(commands.add_parser("edit", help="Validate and apply a batch of edits by instance ID"))
    inspect_project.add_arguments(commands.add_parser("inspect", help="Summarize a project and query placements"))
    search = action("search", "purpose")
    search.add_argument("--dimensions", type=float, nargs=3, help="Target dimensions in metres")
    search.add_argument("--tolerance", type=float, help="Maximum difference per dimension, in metres")
    search.add_argument("--max-weight", type=float, help="Maximum profile weight per candidate")
    search.add_argument("--limit", type=int, default=15)
    card = action("asset")
    card.add_argument("objectId", type=int)
    card.add_argument("--obj", help="Export a complete mesh for 3D inspection")
    card.add_argument("--thumbnail", help="Save the catalog's candidate thumbnail")
    card.add_argument("--textures", default=TEXTURES)
    for sub in (search, card):
        sub.add_argument("--meshes", help="Optional geometry ZIP for exact dimensions")
        sub.add_argument("--profile", default=PROFILE)
        sub.add_argument("--bindings", type=Path, help="JSON with confirmed material paths and their ZIP entries")

    check = commands.add_parser("validate", help="Validate a project or a written PUGC map")
    validate_pugc.add_arguments(check)
    blender = commands.add_parser("blender", help="Run an optional geometry workflow in Blender")
    blender_cli.add_arguments(blender)
    measurement_cli.add_commands(commands)
    three_cli.add_commands(commands)
    return parser, commands.choices


COMMAND_HELP = {
    "init": "Create an editable project",
    "import": "Create an editable project from a PUGC map",
    "build": "Write a map and its companion JSON",
    "recover": "Recover an interrupted build",
    "game-json": "Export game-coordinate JSON",
    "migrate": "Convert a legacy authoring project to game space",
    "place": "Add an instance to the project",
    "group": "Assign instances to an assembly",
    "rotate": "Rotate an assembly",
    "approve-road": "Record the approved road placements",
    "search": "Search the asset catalog",
    "asset": "Inspect an asset and optionally export its geometry or thumbnail",
}


def main(argv=None, *, prog=None):
    arguments = list(sys.argv[1:] if argv is None else argv)
    machine = arguments[:1] == ["--json"]
    parser, commands = _parser(prog, machine)
    if not machine:
        return _run(parser.parse_args(arguments), parser, commands)

    args = None
    try:
        if arguments[1:] == ["--version"]:
            result = cli.response({"version": __version__})
        else:
            args = parser.parse_args(arguments)
            if args.version:
                parser.error("Use --version without a command")
            result = _run(args, parser, commands)
    except cli.HelpRequested as exc:
        result = cli.response({"help": str(exc)})
    except cli.INPUT_ERRORS as exc:
        path = None if args is None else (
            getattr(args, "project", None) or getattr(args, "pugc", None) or getattr(args, "template", None))
        result = cli.from_error(exc, path=path)
    except Exception:
        traceback.print_exc(file=sys.stderr)
        result = cli.response(diagnostics=[
            cli.diagnostic("internal_error", "The command encountered an unexpected error.",
                           "Review stderr and report the command and toolkit version.")
        ], exit_code=cli.FAILED)
    try:
        cli.write_response(result, sys.stdout)
    except ValueError as exc:
        result = cli.response(artifacts=result["artifacts"], diagnostics=[
            cli.diagnostic("invalid_response_data", str(exc), "Check the input for non-finite numbers.")
        ], exit_code=cli.INVALID)
        cli.write_response(result, sys.stdout)
    return result["exit_code"]


def _run(args, parser, commands):
    from . import asset_bundle, asset_packs, batch_edit, blender_cli, inspect_project, measurement_cli, three_cli, validate_pugc

    command = args.command
    sub = commands[command]
    if command in {"terrain", "terrain-area", "area", "measure-gap"}:
        return measurement_cli.run(args)
    if command in {"preview", "compare", "audit", "flythrough"}:
        return three_cli.run(args, sub)
    if command == "capabilities":
        result = {
            "version": __version__, "response_schema": cli.SCHEMA_VERSION, "project_schema": PROJECT_SCHEMA, "coordinate_space": SPACE,
            "pugc_version": CONTAINER_VERSION, "commands": list(commands),
            "blender_operations": list(blender_cli.OPERATIONS),
            "available": {**{name: shutil.which(name) is not None for name in ("openssl", "blender", "ffmpeg")},
                          "numpy": find_spec("numpy") is not None,
                          "trimesh": find_spec("trimesh") is not None,
                          "playwright": find_spec("playwright") is not None,
                          "chromium_on_path": any(shutil.which(name) for name in ("chromium", "google-chrome")),
                          "web_bundle": (three_cli.STATIC / "render.html").is_file()},
            "scene_schema": 1, "receipt_schema": PROJECT_SCHEMA,
        }
        if args.json:
            return cli.response(result)
        print(json.dumps(result, indent=2))
        return 0
    if command == "validate":
        return validate_pugc.run(args, sub)
    if command == "blender":
        return blender_cli.run(args, sub)
    if command == "edit":
        return batch_edit.run(args)
    if command == "inspect":
        return inspect_project.run(args)
    if command in {"doctor", "fetch-pack"}:
        return asset_packs.run(args, sub)
    if command == "fetch-assets":
        return asset_bundle.run(args)
    if command in {"search", "asset"}:
        from contextlib import nullcontext
        from zipfile import ZipFile

        if command == "search":
            if args.max_weight is not None and (not isfinite(args.max_weight) or args.max_weight < 0):
                sub.error("--max-weight requires a finite nonnegative value")
            if not 1 <= args.limit <= 100:
                sub.error("--limit must be between 1 and 100")
            if args.dimensions is not None and not all(isfinite(size) and size > 0 for size in args.dimensions):
                sub.error("--dimensions requires three positive finite sizes in metres")
            if args.tolerance is not None and (args.dimensions is None or not isfinite(args.tolerance)
                                               or args.tolerance < 0):
                sub.error("--tolerance requires --dimensions and a finite nonnegative value in metres")
        profile = load_json(args.profile)
        profiles = profile["objects"]
        index = load_json(CATALOG / "texture_id_index.json")
        materials = load_json(CATALOG / "texture_material_candidates.json")
        bindings = load_json(args.bindings) if args.bindings is not None else {}
        if not isinstance(bindings, dict) or not all(isinstance(value, str) and value for value in bindings.values()):
            sub.error("--bindings requires a JSON object mapping material paths to nonempty ZIP entry paths")
        card_options = {"profile": profile, "materials": materials, "bindings": bindings}
        archive = args.meshes or (MESHES if command == "asset" and args.obj else None)
        if command == "asset" and args.thumbnail:
            cli.archive_argument(args, parser, "--textures")
        library_context = nullcontext()
        if archive:
            args.meshes = archive
            cli.archive_argument(args, parser, "--meshes")
            try:
                from .mesh_library import MeshLibrary
            except ModuleNotFoundError as exc:
                if exc.name != "numpy":
                    raise
                message = "Reading meshes requires NumPy. Install the toolkit artifact with the [geometry] extra."
                if args.json:
                    raise cli.CliError(cli.RESOURCE, "numpy_missing", message,
                                       "Install the toolkit with the [geometry] extra.") from exc
                parser.error(message)
            library_context = MeshLibrary(archive, authoring=True)
        with library_context as library:
            if command == "search":
                search = search_catalog(args.purpose, args.dimensions, args.limit, profiles, index, library,
                                        tolerance=args.tolerance, max_weight=args.max_weight, **card_options)
                result = search["results"]
                diagnostics = []
                if search["query_status"] == "unsupported_term":
                    categories = ", ".join(sorted({entry["category"] for entry in profiles.values()
                                                   if entry.get("category")}))
                    diagnostics.append(cli.diagnostic(
                        "unsupported_search_term", "The query contains an unsupported search term.",
                        f"Search by object ID, use a documented term, or choose a category: {categories}.",
                        field="purpose", severity="warning",
                    ))
                if args.json:
                    search["catalog_coverage"] = coverage(profiles, index, materials, library)
                    return cli.response(search, diagnostics=diagnostics)
                for item in diagnostics:
                    print(item["message"], item["action"], file=sys.stderr)
            else:
                if args.json and str(args.objectId) not in profiles and str(args.objectId) not in index:
                    raise cli.CliError(cli.RESOURCE, "asset_unknown", f"Unknown asset {args.objectId}",
                                       "Use pugc search to select a catalog ID.",
                                       field="objectId", object_id=args.objectId)
                result = asset_card(args.objectId, profiles, index, library, **card_options)
                artifacts = {}
                if args.obj:
                    if args.json and args.objectId not in library.ids:
                        return cli.response(diagnostics=[cli.missing_mesh(archive, object_id=args.objectId)],
                                            exit_code=cli.RESOURCE)
                    export_obj(library.geometry(args.objectId), args.obj)
                    result["whole_model_obj"] = str(args.obj)
                    artifacts["obj"] = args.obj
                if args.thumbnail:
                    try:
                        if not result["thumbnail"]:
                            if not args.json:
                                raise ValueError("No catalog thumbnail candidate")
                            raise cli.CliError(cli.RESOURCE, "thumbnail_missing", "No catalog thumbnail candidate.",
                                               "Use pugc search to choose an asset with a thumbnail.",
                                               field="thumbnail", object_id=args.objectId)
                        with ZipFile(args.textures) as textures:
                            thumbnail = textures.read(result["thumbnail"])
                        Path(args.thumbnail).write_bytes(thumbnail)
                    except cli.INPUT_ERRORS as exc:
                        if not args.json:
                            raise
                        if isinstance(exc, KeyError):
                            exc = cli.CliError(cli.RESOURCE, "thumbnail_missing", str(exc),
                                               "Pass a ZIP containing the catalog image with --textures.",
                                               path=args.textures, field="--textures", object_id=args.objectId)
                        return cli.from_error(exc, path=args.textures, artifacts=artifacts)
                    artifacts["thumbnail"] = args.thumbnail
        if args.json:
            return cli.response(result, artifacts=artifacts)
        print(json.dumps(result, indent=2))
        return
    if command == "init":
        source = args.template or args.version_from
        try:
            paths = output_paths((args.project,), inputs=(args.template or TEMPLATE, args.version_from))
            with lock_outputs(paths):
                create = new_project if args.legacy_layout else new_game_project
                project = create(args.name, args.template, version_from=args.version_from)
                save_json(paths[0], project)
        except (OSError, ValueError) as exc:
            if args.json:
                return cli.from_error(exc, path=source)
            sub.error(str(exc))
        if args.json:
            return cli.response({"name": project["game"]["name"]}, artifacts={"project": paths[0]})
        return
    elif command == "import":
        try:
            adjacent = _sidecar_path(args.pugc)
            paths = output_paths((args.project,), inputs=(args.pugc, adjacent, args.receipt))
            with lock_outputs(paths):
                raw, receipt = raw_with_receipt(args.pugc, args.receipt, args.origin_x_cm,
                                                ignore_receipt=args.ignore_receipt)
                if args.legacy_layout:
                    if is_native(receipt):
                        raise ValueError("Use the game-space importer for a schema 2 receipt")
                    project = project_from_map(raw, receipt["origin_x_cm"],
                                               receipt=receipt if "schema" in receipt else None)
                else:
                    project = native_project_from_map(raw, receipt if "schema" in receipt else None)
                project["import_source"] = {"pugc": str(Path(args.pugc).resolve()),
                                            "receipt": str((args.receipt or adjacent).resolve())}
                save_json(paths[0], project)
        except (OSError, ValueError, LookupError, TypeError, OverflowError) as exc:
            if args.json:
                return cli.from_error(exc, path=args.pugc, code="invalid_map")
            if not isinstance(exc, (OSError, ValueError)):
                raise
            sub.error(str(exc))
        if args.json:
            return cli.response({"pugc_sha256": receipt["pugc_sha256"], "instances": len(project["instances"]),
                                 "used_receipt": "schema" in receipt}, artifacts={"project": paths[0]})
        return
    elif command == "recover":
        try:
            paths = _map_outputs(args.pugc, args.raw_json)
            with lock_outputs(paths):
                phase = recover_outputs(paths)
        except (OSError, ValueError) as exc:
            if args.json:
                return cli.from_error(exc, path=args.pugc)
            sub.error(str(exc))
        status = "none" if phase is None else "committed" if phase == "committed" else "restored"
        if args.json:
            artifacts = {key: path for key, path in zip(("pugc", "receipt", "raw_json"), paths)
                         if phase is not None and path.is_file()}
            return cli.response({"publication": status}, artifacts=artifacts)
        print(json.dumps({"status": status}))
        return
    elif command == "build":
        try:
            project = load_json(args.project)
        except (OSError, ValueError) as exc:
            if not args.json:
                raise
            return cli.from_error(exc, path=args.project, code="invalid_project")
        try:
            build_version = _read_build_version(args.version_from) if args.version_from is not None else None
        except (OSError, PugcError) as exc:
            if args.json:
                return cli.from_error(exc, path=args.version_from)
            sub.error(str(exc))
        try:
            paths = _map_outputs(args.pugc, args.raw_json,
                                 inputs=(args.project, args.profile, args.version_from, *_import_sources(project)))
            receipt, report = _checked_build(project, paths, load_json(args.profile), args.allowed_ids,
                                             build_version=build_version)
        except BuildValidationError as exc:
            if args.json:
                return cli.validation_response(exc.report, args.project, "invalid_project")
            print(json.dumps(exc.report, ensure_ascii=False, indent=2), file=sys.stderr)
            raise SystemExit(1)
        except (OSError, ValueError) as exc:
            if args.json:
                return cli.from_error(exc, path=args.pugc)
            sub.error(str(exc))
        if args.json:
            artifacts = dict(zip(("pugc", "receipt", "raw_json"), paths))
            return cli.validation_response(report, args.project, "invalid_project",
                                           data={"pugc_sha256": receipt["pugc_sha256"]}, artifacts=artifacts)
        if report["warnings"]:
            print(json.dumps(report, ensure_ascii=False, indent=2), file=sys.stderr)
        print(receipt["pugc_sha256"])
        return
    elif command == "migrate":
        from .project_migration import migrate

        project = migrate(load_json(args.project))
        (output,) = output_paths((args.output,), inputs=(args.project, *_import_sources(project)))
        with lock_outputs((output,)):
            save_json(output, project)
        if args.json:
            return cli.response({"coordinate_space": SPACE}, artifacts={"project": output})
        return 0
    elif command == "game-json":
        project = load_json(args.project)
        try:
            paths = output_paths((args.output,), inputs=(args.project, *_import_sources(project)))
            with lock_outputs(paths):
                save_json(paths[0], game_records(project))
        except (OSError, ValueError) as exc:
            if args.json:
                return cli.from_error(exc, path=args.project, code="invalid_project")
            sub.error(str(exc))
        if args.json:
            return cli.response(artifacts={"game_json": paths[0]})
        return
    if command == "place":
        from .inspect_project import read_project

        project, revision = read_project(args.project)
        if is_native(project):
            if args.at_m is not None:
                sub.error("Game-space projects require --at-cm")
            fields = {"objectId": args.objectId, "at_cm": args.at_cm or args.at,
                      "yaw_deg": args.yaw_deg, "scale": list(args.scale),
                      "assembly": args.assembly, "zone": args.zone}
            fields.update({name: value for name in ("source", "role") if (value := getattr(args, name)) is not None})
            result = batch_edit.edit_request(args.project, {"schema": 1, "base_sha256": revision,
                                             "operations": [{"op": "add", "id": args.id, "fields": fields}]})
            if args.json:
                if result["exit_code"]:
                    return result
                return cli.response({"instance_id": args.id, "object_id": args.objectId},
                                    artifacts={"project": args.project}, diagnostics=result["diagnostics"])
            if result["diagnostics"]:
                print(json.dumps(result["diagnostics"], ensure_ascii=False), file=sys.stderr)
            return result["exit_code"]
    warnings = []
    if command == "place" and scaled_building(load_json(PROFILE)["objects"].get(str(args.objectId), {}), args.scale):
        warnings.append(cli.diagnostic("house_scale", HOUSE_SCALE_WARNING, "Use scale [1, 1, 1].",
                                       path=args.project, field="scale", instance_id=args.id, object_id=args.objectId,
                                       severity="warning"))
    try:
        paths = output_paths((args.project,))
        with lock_outputs(paths):
            _edit_project(args, paths[0])
    except (PublicationError, ValueError) as exc:
        if args.json:
            raise
        parser.error(str(exc))
    if args.json:
        data = {"instance_id": args.id, "object_id": args.objectId} if command == "place" else {}
        return cli.response(data, artifacts={"project": paths[0]}, diagnostics=warnings)
    if warnings:
        print(json.dumps(warnings, ensure_ascii=False), file=sys.stderr)


def _edit_project(args, path):
    project = load_json(path)
    command = args.command
    if command == "place":
        if is_native(project):
            raise ValueError("Project format changed while opening it. Retry the placement.")
        if args.at_cm is not None:
            raise ValueError("Legacy projects require --at-m or migration to game space")
        place(project, args.id, args.objectId, transform(args.at_m or args.at, args.yaw_deg, args.scale),
              args.assembly, args.zone, args.source, args.role)
    if command == "group":
        entries = [instance(project, key) for key in args.ids]
        if any(entry is None for entry in entries):
            raise ValueError("Unknown instance in group")
        for entry in entries:
            entry["assembly"] = args.assembly
    if command == "rotate":
        center = args.center_cm if is_native(project) else args.center
        if center is None:
            raise ValueError("Use --center-cm for game-space projects or --center for legacy projects")
        rotate_assembly(project, args.assembly, center, args.yaw)
    if command == "approve-road":
        approve_road(project)
    save_json(path, project)


if __name__ == "__main__":
    raise SystemExit(main())
