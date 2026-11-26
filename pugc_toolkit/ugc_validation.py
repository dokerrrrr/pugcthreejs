"""Project structure and game placement rules shared by validation and build."""
from collections import Counter
from math import isclose, isfinite
import re

from .game_space import PROJECT_SCHEMA, SPACE, is_native, transform_key
from .pugc_codec import LAYOUT_VERSION

ZONES = ("road", "shoulder", "near", "far")
TRANSFORM_AXES = (("translation", "xyz"), ("rotation", "xyzw"), ("scale3D", "xyz"))
HOUSE_SCALE_WARNING = "Keep enterable buildings at native scale (1, 1, 1)."
BUILDING_NAMES = {"apartment", "barrack", "barracks", "building", "cathedral", "church", "commercial",
                  "diner", "factory", "farmhouse", "garage", "hall", "hospital", "hotel", "house", "hut",
                  "mansion", "motel", "office", "police", "residential", "school", "shed", "storehouse",
                  "supermarket", "theater", "toilet", "toliet", "warehouse"}
SCENERY_NAMES = {"addon", "cover", "deck", "door", "dummy", "facade", "fence", "pillar", "roof",
                 "ruins", "shell", "wall"}


def enterable_building(meta):
    if type(meta.get("enterable")) is bool:
        return meta["enterable"]
    if meta.get("category") not in {"SmallHouse", "MediumHouse", "LargeHouse"}:
        return False
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", meta.get("name") or "")
    name = re.sub(r"_mod_?house$", "", name.casefold())
    words = set(re.findall(r"[a-z]+", name))
    return bool(words & BUILDING_NAMES) and not words & SCENERY_NAMES


def scaled_building(meta, scale):
    return len(scale) == 3 and enterable_building(meta) and any(
        not isclose(value, 1, abs_tol=1e-7, rel_tol=0) for value in scale)


def finite(value):
    if isinstance(value, int) and not isinstance(value, bool):
        try:
            return isfinite(value)
        except OverflowError:
            return False
    return isinstance(value, float) and isfinite(value)


def transform_diagnostics(value, label):
    if not isinstance(value, dict):
        return [f"{label}: expected an object"], []
    errors, scales = [], []
    for field, axes in TRANSFORM_AXES:
        vector = value.get(field)
        components = [vector.get(axis) for axis in axes] if isinstance(vector, dict) else []
        if not components or not all(finite(number) for number in components):
            errors.append(f"{label}.{field}: expected finite {axes}")
        elif field == "rotation" and not isclose(sum(float(number) * number for number in components),
                                                 1, rel_tol=.01):
            errors.append(f"{label}.rotation: quaternion is outside unit tolerance")
        elif field == "scale3D":
            scales = components
    return errors, scales


def _project_references(project, ids):
    references, errors = [], []
    for field in ("road_joins", "landmarks", "opaque_instances"):
        records = project.get(field, [])
        if not isinstance(records, list):
            errors.append(f"{field}: expected an array")
            continue
        for index, record in enumerate(records):
            path = f"{field}[{index}]"
            if field == "opaque_instances":
                references.append((record, path))
            elif not isinstance(record, dict):
                errors.append(f"{path}: expected an object")
            elif field == "road_joins":
                references.extend((record.get(key), f"{path}.{key}") for key in ("from", "to"))
            elif isinstance(record.get("result"), list):
                references.extend((key, f"{path}.result[{i}]") for i, key in enumerate(record["result"]))
            else:
                errors.append(f"{path}.result: expected an array of instance IDs")
    for value, path in references:
        if not isinstance(value, str) or value not in ids:
            errors.append(f"{path}: unknown instance {value!r}")
    return errors


def project_errors(project):
    if not isinstance(project, dict):
        return ["project: expected an object"]
    errors = []
    if type(project.get("schema")) is not int or project["schema"] not in (1, PROJECT_SCHEMA):
        errors.append("schema: unsupported project version (expected 1 or 2)")
    native, field = is_native(project), transform_key(project)
    if native and (project.get("coordinate_space") != SPACE or {"origin_x_cm", "layout_version"} & project.keys()):
        errors.append("coordinate_space: expected game-xyz-cm for schema 2")
    if "layout_version" in project and (type(project["layout_version"]) is not int
                                        or project["layout_version"] != LAYOUT_VERSION):
        errors.append(f"layout_version: unsupported version (expected {LAYOUT_VERSION})")
    if not native and not finite(project.get("origin_x_cm")):
        errors.append("origin_x_cm: expected a finite number")
    if not isinstance(project.get("game"), dict):
        errors.append("game: expected an object")
    entries = project.get("instances")
    if not isinstance(entries, list):
        return [*errors, "instances: expected an array"]

    by_id = {}
    for index, entry in enumerate(entries):
        path = f"instances[{index}]"
        if not isinstance(entry, dict):
            errors.append(f"{path}: expected an object")
            continue
        key = entry.get("id")
        label = f"{path} ({key!r})" if isinstance(key, str) else path
        if not isinstance(key, str) or not key or key in by_id:
            errors.append(f"{label}.id: expected a nonempty, unique ID")
        else:
            by_id[key] = entry
        if type(entry.get("objectId")) is not int:
            errors.append(f"{label}.objectId: expected integer")
        if not isinstance(entry.get("assembly"), str) or not entry["assembly"]:
            errors.append(f"{label}.assembly: expected a nonempty string")
        if entry.get("zone") not in ZONES:
            errors.append(f"{label}.zone: expected road, shoulder, near or far")
        errors.extend(transform_diagnostics(entry.get(field), f"{label}.{field}")[0])
        if "game_original" in entry:
            original = entry["game_original"]
            if not isinstance(original, dict):
                errors.append(f"{label}.game_original: expected an object")
            else:
                if type(original.get("objectId")) is not int:
                    errors.append(f"{label}.game_original.objectId: expected integer")
                errors.extend(transform_diagnostics(original.get("spawnTransform"),
                                                    f"{label}.game_original.spawnTransform")[0])

    approvals = project.get("approved_road", {})
    if not isinstance(approvals, dict):
        errors.append("approved_road: expected an object")
    else:
        for key, approval in approvals.items():
            entry = by_id.get(key)
            if entry is None:
                errors.append(f"approved_road[{key!r}]: approved instance is missing")
            elif entry.get("zone") != "road" or approval != {"objectId": entry.get("objectId"),
                                                              field: entry.get(field)}:
                errors.append(f"approved_road[{key!r}]: approved road instance changed")
    errors.extend(_project_references(project, by_id))
    return errors


def budget_rows(objects, profile, zones=None):
    entries = [profile["objects"].get(str(obj.get("objectId"))) for obj in objects]
    priceable = [entry is not None and finite(entry.get("weight")) and entry["weight"] > 0
                 for entry in entries]
    rows = []
    for rule in profile["rules"]:
        totals = {zone: 0 for zone in (*ZONES, "unclassified")}
        count = unpriced = 0
        for index, (obj, meta, has_price) in enumerate(zip(objects, entries, priceable)):
            matches = rule.get("match") == "all" or meta and all(
                field not in rule or rule[field] == actual
                for field, actual in (("objectType", meta.get("type")),
                                      ("subCategory", meta.get("category")),
                                      ("objectId", obj.get("objectId"))))
            if not matches:
                continue
            count += 1
            if not has_price:
                unpriced += 1
                continue
            zone = zones[index] if zones is not None and zones[index] in ZONES else "unclassified"
            totals[zone] += meta["weight"]
        weight = sum(totals.values())
        limit = rule.get("limit")
        rows.append({**rule, "count": count, "weight": weight, "unpriced": unpriced, "zones": totals,
                     "remaining": limit - weight if limit is not None else None,
                     "available_after_road_and_shoulder": limit - totals["road"] - totals["shoulder"]
                     if limit is not None else None, "complete": all(priceable)})
    return rows


def game_header_diagnostics(data, profile, *, imported=False):
    errors, warnings = [], []
    for key, expected in (("name", str), ("dataVersion", int), ("buildVersion", str),
                          ("deviceEventList", list)):
        if not isinstance(data.get(key), expected):
            message = f"{key}: expected {expected.__name__}"
            if key == "deviceEventList" and key not in data and imported:
                warnings.append(f"{message} (inherited missing field)")
            else:
                errors.append(message)
    if data.get("dataVersion") != profile["format_version"]:
        errors.append(f"dataVersion: expected {profile['format_version']} for this profile")
    return errors, warnings


def validate(data, profile, zones=None, allowed_ids=None, *, originals=None, labels=None, strict_scale=True):
    errors, warnings = game_header_diagnostics(data, profile, imported=bool(originals and any(originals)))
    objects = data.get("objects")
    if not isinstance(objects, list):
        errors.append("objects: expected list")
        objects = []
    allowed_ids = set(allowed_ids) if allowed_ids is not None else None
    counted = Counter()
    scales = []
    devices = 0
    device_instances = set()
    for index, value in enumerate(objects):
        label = labels[index] if labels is not None else f"objects[{index}]"
        if not isinstance(value, dict):
            errors.append(f"{label}: expected an object record")
            continue
        baseline = originals[index] if originals is not None else None
        inherited = value == baseline
        oid = value.get("objectId")
        if type(oid) is not int:
            errors.append(f"{label}.objectId: expected integer")
            meta = None
        else:
            counted[oid] += 1
            meta = profile["objects"].get(str(oid))
            if allowed_ids is not None and oid not in allowed_ids:
                errors.append(f"{label}.objectId: {oid} outside allowed IDs")
            if meta is None:
                message = f"{label}.objectId: {oid} outside known profile"
                if inherited:
                    warnings.append(f"{message} (inherited, budget incomplete)")
                else:
                    errors.append(message)
        device = value.get("deviceIndex")
        if type(device) is not int or device < -1:
            errors.append(f"{label}.deviceIndex: expected integer ≥ -1")
        else:
            devices += device >= 0
            if device >= 0:
                device_instances.add((oid if type(oid) is int else None, device))
            if meta and (device >= 0) != (meta.get("type") == "Device"):
                errors.append(f"{label}: deviceIndex disagrees with profile type {meta.get('type')}")
        if not isinstance(value.get("userDeviceName"), str):
            errors.append(f"{label}.userDeviceName: expected string")
        if meta and (not meta.get("category") or meta.get("type") not in {"Prop", "Device"}
                     or not finite(meta.get("weight")) or meta["weight"] <= 0):
            errors.append(f"{label}: profile has invalid category or placement weight")
        t = value.get("spawnTransform")
        problems, values = transform_diagnostics(t, f"{label}.spawnTransform")
        errors.extend(problems)
        scales.extend(values)
        if meta and scaled_building(meta, values):
            warnings.append(f"{label}.spawnTransform.scale3D: {HOUSE_SCALE_WARNING}")
        places = profile["scale_decimal_places"]
        if any(n < profile["scale_min"] or not isclose(n, round(n, places), abs_tol=1e-7, rel_tol=0)
               for n in values):
            message = f"{label}.spawnTransform.scale3D: expected ≥ {profile['scale_min']} and {places} decimals"
            if (baseline and baseline["objectId"] == oid
                    and baseline["spawnTransform"]["scale3D"] == t["scale3D"]):
                warnings.append(f"{message} (inherited)")
            elif not strict_scale and all(n > 0 for n in values):
                warnings.append(f"{message} (outside the authoring profile)")
            else:
                errors.append(message)
    safe_objects = [entry if isinstance(entry, dict) else {} for entry in objects]
    rows = budget_rows(safe_objects, profile, zones)
    original_rows = (budget_rows([obj for obj in originals if obj is not None], profile)
                     if originals is not None else [])
    for index, row in enumerate(rows):
        if row["remaining"] is not None and row["remaining"] < 0:
            message = f"{row['label']}: {row['weight']}/{row['limit']} placement weight"
            if original_rows and row["weight"] <= original_rows[index]["weight"]:
                warnings.append(f"{message} (inherited)")
            else:
                errors.append(message)
    events = data.get("deviceEventList")
    if isinstance(events, list):
        errors.extend(_device_event_errors(events, device_instances))
    return {"name": data.get("name"), "dataVersion": data.get("dataVersion"),
            "buildVersion": data.get("buildVersion"), "profile": profile["id"],
            "profile_revision": profile["revision"], "sources": profile["sources"],
            "objects": len(objects), "distinct_ids": len(counted), "ids": dict(sorted(counted.items())),
            "devices": devices, "deviceEvents": len(events) if isinstance(events, list) else None,
            "scaleMinimum": min(scales) if scales else None, "scaleMaximum": max(scales) if scales else None,
            "budget": rows, "errors": errors, "warnings": warnings, "valid": not errors}


def _device_event_errors(events, device_instances):
    errors = []
    indexes = {index for _, index in device_instances}
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            continue
        references = [("eventId", event.get("eventId"))]
        relations = event.get("relationEventId")
        if isinstance(relations, list):
            references.extend((f"relationEventId[{i}]", relation) for i, relation in enumerate(relations))
        for path, endpoint in references:
            if isinstance(endpoint, dict) and "deviceInstanceId" in endpoint:
                device = endpoint["deviceInstanceId"]
                if isinstance(device, dict):
                    parts = (device.get("objectId"), device.get("deviceIndex"))
                    known = all(type(part) is int for part in parts) and parts in device_instances
                else:
                    known = type(device) is int and device in indexes
                if not known:
                    errors.append(f"deviceEventList[{index}].{path}.deviceInstanceId: unknown device {device!r}")
    return errors
