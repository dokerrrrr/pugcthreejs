"""Validate and apply a revision-bound list of instance edits."""
from contextlib import nullcontext
from hashlib import sha256
import json
from pathlib import Path
import re

from . import cli_protocol as cli
from .game_space import transform_cm, transform_key
from .resources import PROFILE
from .ugc_project import check_project_output, load_json, vector
from .ugc_storage import atomic_write, json_bytes, lock_outputs, output_paths
from .ugc_validation import ZONES, finite, project_errors

TRANSFORMS = {
    "at_cm": ("translation", "xyz", 1),
    "at_m": ("translation", "xyz", 100),
    "rotation_xyzw": ("rotation", "xyzw", 1),
    "scale": ("scale3D", "xyz", 1),
}
METADATA = {"objectId", "assembly", "zone", "source", "role"}
DEFAULT_FIELDS = {"zone": "near", "rotation_xyzw": [0, 0, 0, 1], "scale": [1, 1, 1]}
MISSING = object()


def add_arguments(parser):
    parser.add_argument("project", type=Path, help="Editable game-space or legacy project")
    parser.add_argument("request", type=Path, help="Batch request with expected project SHA-256")
    parser.add_argument("--dry-run", action="store_true", help="Validate and list changes without writing")
    parser.add_argument("--profile", default=PROFILE, help="Placement profile used to validate the resulting map")
    parser.add_argument("--meshes", type=Path, help="Mesh ZIP for relative placement")
    parser.add_argument("--base-level", type=Path, help="Local verified island GLB")
    parser.add_argument("--allowed-ids", nargs="*", type=int, help="Restrict catalog object IDs")


def _invalid(path, field, message, *, instance_id=None):
    raise cli.CliError(cli.INVALID, "invalid_edit", message, "Correct the batch request and retry.",
                       path=path, field=field, instance_id=instance_id)


def _request(request, path):
    if not isinstance(request, dict) or request.keys() != {"schema", "base_sha256", "operations"}:
        _invalid(path, "request", "Expected exactly schema, base_sha256 and operations.")
    if type(request["schema"]) is not int or request["schema"] != 1:
        _invalid(path, "schema", "Expected batch request schema 1.")
    digest = request["base_sha256"]
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        _invalid(path, "base_sha256", "Expected a 64-digit SHA-256 of the current project file.")
    if not isinstance(request["operations"], list):
        _invalid(path, "operations", "Expected an array of operations.")
    return digest.lower(), request["operations"]


def _updated(entry, fields, error, transform_field):
    updated = {**entry, transform_field: dict(entry[transform_field])}
    if "yaw_deg" in fields:
        yaw = fields["yaw_deg"]
        if transform_field == "transform" or not finite(yaw) or "rotation_xyzw" in fields:
            error("yaw_deg", "Expected a finite game yaw without rotation_xyzw.")
        rotation = transform_cm((0, 0, 0), yaw)["rotation"]
        fields = {**{key: value for key, value in fields.items() if key != "yaw_deg"},
                  "rotation_xyzw": [rotation[axis] for axis in "xyzw"]}
    for field, value in fields.items():
        if ((field == "at_m" and transform_field != "transform") or
                (field == "at_cm" and transform_field == "transform")):
            error(field, "Use game at_cm for schema 2 and authoring at_m for schema 1.")
        if field in TRANSFORMS:
            name, axes, ratio = TRANSFORMS[field]
            if (not isinstance(value, list) or len(value) != len(axes)
                    or not all(finite(component) and finite(component * ratio) for component in value)):
                error(field, f"Expected {len(axes)} finite numeric components.")
            coordinates = {axis: component * ratio for axis, component in zip(axes, value, strict=True)}
            transform = updated[transform_field]
            transform[name] = {**transform.get(name, {}), **coordinates}
        elif field == "objectId" and type(value) is int:
            updated[field] = value
        elif field == "zone" and value in ZONES:
            updated[field] = value
        elif field in {"assembly", "source", "role"} and isinstance(value, str) and value:
            updated[field] = value
        elif field in {"source", "role"} and value is None:
            updated.pop(field, None)
        else:
            error(field, f"Invalid value for {field}.")
    return updated


def _apply_operation(entries, op, index, path, original_ids, transform_field):
    label = f"operations[{index}]"
    if not isinstance(op, dict):
        _invalid(path, label, "Expected an operation object.")
    kind, key = op.get("op"), op.get("id")
    if kind not in ("add", "update", "delete"):
        _invalid(path, f"{label}.op", "Expected add, update or delete.")
    if not isinstance(key, str) or not key:
        _invalid(path, f"{label}.id", "Expected a nonempty stable instance ID.")
    fields_required = kind != "delete"
    if op.keys() != ({"op", "id", "fields"} if fields_required else {"op", "id"}):
        _invalid(path, label, "Check operation keys in the edit command reference.", instance_id=key)
    if (key in entries) == (kind == "add"):
        message = "Instance already exists." if kind == "add" else "Instance does not exist."
        _invalid(path, f"{label}.id", message, instance_id=key)
    if kind == "add" and key in original_ids:
        _invalid(path, f"{label}.id", "An original ID cannot be reused after deletion in the same batch.",
                 instance_id=key)
    if kind == "delete":
        del entries[key]
    else:
        fields = op["fields"]
        if not isinstance(fields, dict) or fields.keys() - (METADATA | TRANSFORMS.keys() | {"yaw_deg"}):
            _invalid(path, f"{label}.fields", "Expected an object containing supported fields.", instance_id=key)
        position = "at_m" if transform_field == "transform" else "at_cm"
        if kind == "add" and not {"objectId", position, "assembly"} <= fields.keys():
            _invalid(path, f"{label}.fields", f"Adding an instance requires objectId, {position} and assembly.",
                     instance_id=key)
        if kind == "add":
            defaults = DEFAULT_FIELDS if "yaw_deg" not in fields else {
                key: value for key, value in DEFAULT_FIELDS.items() if key != "rotation_xyzw"
            }
            fields = {**defaults, **fields}

        def error(field, message):
            _invalid(path, f"{label}.fields.{field}", message, instance_id=key)

        entries[key] = _updated(entries.get(key, {"id": key, transform_field: {}}), fields, error, transform_field)
    return key


def _changes(before, after, touched, transform_field):
    changes = []
    for key in dict.fromkeys(touched):
        prior, current = before.get(key), after.get(key)
        if prior == current:
            continue
        if prior is None or current is None:
            changes.append({"id": key, "change": "add" if prior is None else "delete", "fields": ["instance"]})
            continue
        fields = sorted(name for name in METADATA if prior.get(name, MISSING) != current.get(name, MISSING))
        fields.extend(f"{transform_field}.{name}" for name in ("translation", "rotation", "scale3D")
                      if prior[transform_field][name] != current[transform_field][name])
        changes.append({"id": key, "change": "update", "fields": fields})
    return changes


def edit(project_path, request_path, *, dry_run=False, profile=PROFILE, allowed_ids=None, geometry=None):
    try:
        request = load_json(request_path)
    except ValueError as exc:
        _invalid(request_path, "request", str(exc))
    return edit_request(project_path, request, request_path=request_path, dry_run=dry_run,
                        profile=profile, allowed_ids=allowed_ids, geometry=geometry)


def _geometry_operation(operation, entries, geometry, index, path, original_ids, transform_field):
    from copy import deepcopy
    from .game_space import TRANSFORM
    from .resources import MESHES
    from .placement_geometry import PlacementGeometry

    label = f"operations[{index}]"
    if transform_field != TRANSFORM:
        _invalid(path, label, "Migrate the project to game space before using relative operations.")
    geometry = geometry or PlacementGeometry(MESHES, None)
    kind, key = operation["op"], operation.get("id")
    support = operation.get("support")
    if support not in (None, "terrain", "water"):
        _invalid(path, f"{label}.support", "Expected terrain or water.")
    if kind == "seat":
        if operation.keys() != {"op", "id", "support"} or support is None or not isinstance(key, str) or key not in entries:
            _invalid(path, label, "Seat requires an existing ID and support.")
        entries[key] = deepcopy(entries[key])
        contact = geometry.seat(entries[key], support)
        return key, {"id": key, TRANSFORM: entries[key][TRANSFORM], "contact": contact}
    if operation.keys() - {"op", "id", "objectId", "anchor_id", "axis", "gap_cm", "support",
                           "assembly", "zone", "yaw_deg", "rotation_xyzw", "scale"}:
        _invalid(path, label, "Unexpected relative placement field.")
    anchor_id, axis, distance = (operation.get(name) for name in ("anchor_id", "axis", "gap_cm"))
    if not isinstance(anchor_id, str) or anchor_id not in entries or not finite(distance) or distance < 0:
        _invalid(path, label, "Expected an existing anchor and a nonnegative gap_cm.")
    fields = {key: operation[key] for key in ("objectId", "assembly", "zone", "yaw_deg", "rotation_xyzw", "scale")
              if key in operation}
    normalized = {"op": "add", "id": key, "fields": {**fields, "at_cm": [0, 0, 0]}}
    _apply_operation(entries, normalized, index, path, original_ids, transform_field)
    measure, contact = geometry.place_relative(entries[key], entries[anchor_id], axis, distance, support)
    return key, {"id": key, "anchor_id": anchor_id, TRANSFORM: entries[key][TRANSFORM],
                 "gap": measure, "contact": contact}


def edit_request(project_path, request, *, request_path=None, dry_run=False, profile=PROFILE, allowed_ids=None,
                 geometry=None):
    wanted, operations = _request(request, request_path)
    (path,) = output_paths((project_path,), inputs=(request_path, profile))
    with nullcontext() if dry_run else lock_outputs((path,)):
        original = path.read_bytes()
        revision = sha256(original).hexdigest()
        if revision != wanted:
            raise cli.CliError(cli.FAILED, "revision_conflict",
                               f"Expected {wanted}, found {revision}.",
                               "Read the current project and prepare a fresh request with its SHA-256.",
                               path=path, field="base_sha256")
        try:
            project = json.loads(original)
        except ValueError as exc:
            return cli.from_error(exc, path=path, code="invalid_project")
        errors = project_errors(project)
        if errors:
            return cli.validation_response({"valid": False, "errors": errors, "warnings": []}, path,
                                           "invalid_project")
        transform_field = transform_key(project)
        before = {row["id"]: row for row in project["instances"]}
        after = before.copy()
        touched, measurements = [], []
        for index, op in enumerate(operations):
            if isinstance(op, dict) and op.get("op") in ("place_relative", "seat"):
                key, measurement = _geometry_operation(op, after, geometry, index, request_path,
                                                       before.keys(), transform_field)
                measurements.append(measurement)
            else:
                key = _apply_operation(after, op, index, request_path, before.keys(), transform_field)
            touched.append(key)
            if key in project.get("approved_road", {}):
                previous, current = before[key], after.get(key)
                if current is None or any(previous[field] != current[field]
                                          for field in ("objectId", "zone", transform_field)):
                    raise cli.CliError(cli.INVALID, "approved_road_changed", "The edit changes an approved road.",
                                       "Keep the approved road placement in this batch.",
                                       path=request_path, field=f"operations[{index}]", instance_id=key)
        changes = _changes(before, after, touched, transform_field)
        project["instances"] = list(after.values())
        _, report = check_project_output(project, load_json(profile), allowed_ids)
        if not report["valid"]:
            return cli.validation_response(report, path, "invalid_project")
        staged = json_bytes(project) if changes else original
        if changes and not dry_run:
            atomic_write(path, staged)
        data = {
            "edit_status": "preview" if dry_run else "applied" if changes else "unchanged",
            "base_sha256": revision,
            "result_sha256": sha256(staged).hexdigest(),
            "changes": changes,
        }
        if measurements:
            data.update(measurements=measurements, coverage="complete")
        return cli.validation_response(report, path, "invalid_project", data=data,
                                       artifacts={"project": path} if changes and not dry_run else None)


def run(args):
    try:
        geometry = None
        if args.meshes or args.base_level:
            from .placement_geometry import PlacementGeometry
            from .resources import MESHES

            geometry = PlacementGeometry(args.meshes or MESHES, args.base_level)
        result = edit(args.project, args.request, dry_run=args.dry_run, profile=args.profile,
                      allowed_ids=args.allowed_ids, geometry=geometry)
    except cli.INPUT_ERRORS as exc:
        result = cli.from_error(exc, path=args.project, code="invalid_edit")
        if result["exit_code"] == cli.RESOURCE:
            result["data"]["coverage"] = "partial"
    if args.json:
        return result
    print(json.dumps(result["data"] if result["exit_code"] == 0 else result["diagnostics"],
                     ensure_ascii=False, indent=2))
    if result["exit_code"] == 0 and result["diagnostics"]:
        import sys

        print(json.dumps(result["diagnostics"], ensure_ascii=False), file=sys.stderr)
    return result["exit_code"]
