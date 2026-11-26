"""Audit road approvals, directed joins and under-road visibility from a written PUGC.

Run on Python with NumPy using a CPU triangle index.
"""
import argparse
from functools import lru_cache
from itertools import product
from pathlib import Path
import sys

import numpy as np

from . import cli_protocol as cli
from . import geometry_report as coverage
from .game_space import is_native, transform_key
from .legacy_layout import legacy_transform, rebase_legacy_transform
from .mesh_library import MeshLibrary
from .pugc_codec import convert_layout
from .project_migration import annotation_origin
from .resources import MESHES, PROFILE
from .triangle_index import TriangleIndex
from .ugc_geometry import apply_matrix, placement_matrix
from .ugc_project import DEFAULT_ORIGIN_X_CM, _approval_matches_map, _layout_transform, load_json, raw_with_receipt, save_json
from .ugc_validation import finite


def bounds_corners(minimum, maximum):
    return np.array(list(product(*zip(minimum, maximum))))


class MapGeometry:
    def __init__(self, placements, library):
        self.placements = placements
        self.library = library
        self.matrices = {key: placement_matrix(entry["spawnTransform"])
                         for key, (entry, _) in placements.items()}
        self.bounds = {}
        dimensions = {}
        for key, (entry, _) in placements.items():
            oid = entry["objectId"]
            if oid not in library.ids:
                continue
            if oid not in dimensions:
                desc, _ = library.load(oid, 1)
                dimensions[oid] = bounds_corners(desc["bounds_min"], desc["bounds_max"])
            points = apply_matrix(dimensions[oid], self.matrices[key])
            self.bounds[key] = (points.min(axis=0), points.max(axis=0))
        self.surface = lru_cache(maxsize=32)(self._surface)

    def _surface(self, key):
        geo = self.library.geometry(self.placements[key][0]["objectId"])
        points = apply_matrix(geo.vertices, self.matrices[key])
        bvh = TriangleIndex(points, geo.triangles)
        return points, geo.triangles, bvh

    def driving_surface(self, key, material):
        points, faces, _ = self.surface(key)
        if material:
            geo = self.library.geometry(self.placements[key][0]["objectId"])
            spans = [faces[s.first_triangle:s.first_triangle + s.triangle_count]
                     for s in geo.sections if s.material.rsplit("/", 1)[-1] == material]
            faces = np.concatenate(spans) if spans else faces[:0]
        else:
            triangles = points[faces]
            normal = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
            faces = faces[normal[:, 2] > .55 * np.linalg.norm(normal, axis=1)]
        return points, faces, TriangleIndex(points, faces)

    def top(self, key, xy, bvh=None):
        low, high = self.bounds[key]
        if np.any(xy < low[:2] - 1e-5) or np.any(xy > high[:2] + 1e-5):
            return None
        if high[2] <= low[2]:
            return None
        if bvh is None:
            _, _, bvh = self.surface(key)
        hit, _, _, _ = bvh.ray_cast((*xy, high[2] + 1), (0, 0, -1), high[2]-low[2]+2)
        return float(hit[2]) if hit is not None else None


def intervals(mesh, at, left, forward, offset, tolerance):
    points, faces, _ = mesh
    xy = points[faces, :2] - at
    across, along = xy @ left - offset, xy @ forward
    relevant = (across.min(axis=1) <= 0) & (across.max(axis=1) >= 0)
    segments = []
    for x, y in zip(across[relevant], along[relevant]):
        crossings = [y[i] for i in range(3) if abs(x[i]) <= 1e-8]
        for i, j in ((0, 1), (1, 2), (2, 0)):
            if (x[i] < 0 < x[j]) or (x[j] < 0 < x[i]):
                crossings.append(y[i] - x[i] * (y[j] - y[i]) / (x[j] - x[i]))
        if len(crossings) > 1:
            segments.append((min(crossings), max(crossings)))
    merged = []
    for start, end in sorted(segments):
        if merged and start <= merged[-1][1] + tolerance:
            merged[-1][1] = max(end, merged[-1][1])
        else:
            merged.append([start, end])
    return merged


def join_report(geo, join):
    a, b = join["from"], join["to"]
    if a not in geo.bounds or b not in geo.bounds:
        return {"from": a, "to": b, "error": "missing_road_mesh"}
    at = np.asarray(join["at_m"][:2], dtype=float)
    forward = np.asarray(join["forward_xy"], dtype=float)
    norm = np.linalg.norm(forward)
    if not np.isfinite(norm) or norm <= 0:
        raise ValueError("A road join requires a nonzero forward_xy")
    forward /= norm
    left = np.array([-forward[1], forward[0]])
    planar, step_tol = join.get("planar_tolerance_m", .02), join.get("height_tolerance_m", .05)
    step = join.get("sample_distance_m", .15)
    width = join["half_width_m"]
    if any(not np.isfinite(x) or x <= 0 for x in (planar, step_tol, step, width)):
        raise ValueError("Road width, tolerances and sample distance must be positive and finite")
    surfaces = join.get("drive_material", {})
    incoming_surface = geo.driving_surface(a, surfaces.get("from"))
    outgoing_surface = geo.driving_surface(b, surfaces.get("to"))
    samples = []
    for name, offset in (("left", width), ("center", 0), ("right", -width)):
        xy = at + left * offset
        incoming = intervals(incoming_surface, at, left, forward, offset, planar)
        outgoing = intervals(outgoing_surface, at, left, forward, offset, planar)
        before = next((span for span in incoming if span[0]-planar <= -step <= span[1]+planar), None)
        after = next((span for span in outgoing if span[0]-planar <= step <= span[1]+planar), None)
        sample = {"lane": name, "findings": []}
        if before is None or after is None:
            sample["findings"].append("missing_incoming_or_outgoing_surface")
        else:
            gap = after[0] - before[1]
            overlap = max(0, min(before[1], after[1]) - max(before[0], after[0]))
            sample.update(gap_m=max(gap, 0), overlap_m=overlap)
            inset = min(planar / 2, step)
            h_before = geo.top(a, xy + forward * min(-inset, before[1] - inset), incoming_surface[2])
            h_after = geo.top(b, xy + forward * max(inset, after[0] + inset), outgoing_surface[2])
            sample.update(incoming_m=h_before, outgoing_m=h_after, step_m=None)
            if h_before is not None and h_after is not None:
                sample["step_m"] = h_after - h_before
                if abs(sample["step_m"]) > step_tol:
                    sample["findings"].append("height_step")
            else:
                sample["findings"].append("missing_seam_height")
            if gap > planar:
                sample["findings"].append("gap")
            if overlap > planar:
                sample["findings"].append("overlap")
                lo, hi = max(before[0], after[0]), min(before[1], after[1])
                separations = []
                for t in (.1, .5, .9):
                    point = xy + forward * (lo + t * (hi-lo))
                    first = geo.top(a, point, incoming_surface[2])
                    second = geo.top(b, point, outgoing_surface[2])
                    if first is not None and second is not None:
                        separations.append(second - first)
                sample["overlap_height_separation_m"] = separations
                if len(separations) >= 2 and (np.max(np.abs(separations)) <= .005
                                              or min(separations) <= 0 <= max(separations)):
                    sample["findings"].append("coplanar_or_intersecting_surface_risk")
        samples.append(sample)
    return {"from": a, "to": b, "at_m": join["at_m"], "forward_xy": forward.tolist(), "samples": samples}


def closed_mesh(points, faces):
    # Weld mesh dump triangle soup within 0.1 mm for edge incidence.
    _, inverse = np.unique(np.round(points - points[0], 4), axis=0, return_inverse=True)
    face = inverse[faces]
    edges = np.sort(np.concatenate([face[:, [0, 1]], face[:, [1, 2]], face[:, [2, 0]]]), axis=1)
    return bool(len(edges) and np.all(np.unique(edges, axis=0, return_counts=True)[1] == 2))


def enclosing_object(geo, opaque_keys, minimum, maximum):
    center = (minimum + maximum) / 2
    radius = np.linalg.norm(maximum - center)
    for key in opaque_keys:
        lo, hi = geo.bounds[key]
        if np.any(center - lo <= radius) or np.any(hi - center <= radius):
            continue
        vertices, faces, bvh = geo.surface(key)
        _, _, _, distance = bvh.find_nearest(center)
        if distance is None or distance <= radius + .002 or not closed_mesh(vertices, faces):
            continue
        inside = True
        for direction in ((.971, .186, .151), (-.232, .923, .307), (.192, -.935, -.299)):
            ray = np.asarray(direction) / np.linalg.norm(direction)
            position = center
            crossings = 0
            while True:
                hit, _, _, _ = bvh.ray_cast(position, ray)
                if hit is None:
                    break
                crossings += 1
                position = hit + ray * .0002
                if crossings >= len(faces):
                    break
            if crossings % 2 == 0:
                inside = False
                break
        if inside:
            return {"instance": key, "clearance_m": float(distance - radius)}
    return None


def ray_box(origin, target, bounds):
    lower, upper = bounds
    delta = target - origin
    nonzero = np.abs(delta) > 1e-10
    if np.any((origin < lower - 1e-5) & ~nonzero) or np.any((origin > upper + 1e-5) & ~nonzero):
        return False
    a = (lower[nonzero] - origin[nonzero]) / delta[nonzero]
    b = (upper[nonzero] - origin[nonzero]) / delta[nonzero]
    return max(0, *np.minimum(a, b)) <= min(1, *np.maximum(a, b))


def route_visibility(geo, key, viewpoints, opaque):
    points, faces, _ = geo.surface(key)
    samples = points[faces[np.linspace(0, len(faces)-1, min(len(faces), 20), dtype=int)]].mean(axis=1)
    rows = []
    for view in viewpoints:
        origin = np.array(view["at_m"], dtype=float)
        visible = False
        for target in samples:
            vec = target - origin
            distance = np.linalg.norm(vec)
            if distance <= .01:
                continue
            direction = vec / distance
            if all(geo.surface(obstacle)[2].ray_cast(origin, direction, distance - .005)[0] is None
                   for obstacle in opaque if obstacle != key and ray_box(origin, target, geo.bounds[obstacle])):
                visible = True
                break
        rows.append({"name": view["name"], "route": view["route"],
                     "status": "visible_sample" if visible else "all_samples_blocked"})
    return rows


def road_approvals(placements, approved, field="transform"):
    errors = []
    for key, expected in approved.items():
        current, meta = placements.get(key, (None, {}))
        if (current is None or current["objectId"] != expected["objectId"] or meta.get("zone") != "road"
                or not np.allclose(placement_matrix(current["spawnTransform"]),
                                   placement_matrix(expected[field]), rtol=0, atol=1e-8)):
            errors.append(key)
    return errors


def add_arguments(parser):
    parser.add_argument("pugc")
    parser.add_argument("--project", required=True)
    parser.add_argument("--meshes", default=MESHES)
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--receipt")
    parser.add_argument("--origin-x-cm", type=float, help="Authoring origin for a map without a receipt")
    parser.add_argument("--out", required=True)
    parser.add_argument("--strict", action="store_true", help="Require complete coverage of the provided annotations")
    parser.add_argument("--require", nargs="+", choices=("road_joins", "viewpoints", "approved_road"),
                        default=(), help="Require these annotation groups in strict mode")

def main(*, machine=False):
    parser_type = cli.MachineParser if machine else argparse.ArgumentParser
    parser = parser_type(prog="pugc blender audit" if machine else None, description=__doc__)
    add_arguments(parser)
    args = parser.parse_args(sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else [])
    return run(args, parser, machine=machine)


def run(args, parser, *, machine):
    cli.archive_argument(args, parser, "--meshes")
    project = load_json(args.project)
    supplied_origin = args.origin_x_cm if args.origin_x_cm is not None else project.get("origin_x_cm")
    raw, receipt = raw_with_receipt(args.pugc, args.receipt, supplied_origin)
    use_legacy_frame = not is_native(project) or "annotation_frame" in project
    origin = annotation_origin(project, receipt.get("origin_x_cm", DEFAULT_ORIGIN_X_CM))
    data = convert_layout(raw, origin) if use_legacy_frame else raw
    annotations = project.get("annotations", {})
    placements = {
        meta["id"]: (entry, {"zone": "near", **meta, **annotations.get(meta["id"], {})})
        for entry, meta in zip(data["objects"], receipt["instances"], strict=True)
    }
    weights = load_json(args.profile)["objects"]
    requested_viewpoints = project.get("viewpoints", [])
    viewpoints, missing_views = [], []
    for index, view in enumerate(requested_viewpoints):
        if (isinstance(view, dict) and view.get("name") and view.get("route")
                and isinstance(view.get("at_m"), list) and len(view["at_m"]) == 3
                and all(finite(value) for value in view["at_m"])):
            viewpoints.append(view)
        else:
            name = view.get("name") if isinstance(view, dict) else None
            missing_views.append(name or f"viewpoints[{index}]")
    approval_source = project if "approved_road" in project else receipt
    approved = approval_source.get("approved_road", {})
    approval_records = placements
    approvals_native = is_native(approval_source)
    if approvals_native != (not use_legacy_frame):
        approval_data = raw if approvals_native else convert_layout(raw, origin)
        approval_records = {meta["id"]: (entry, placements[meta["id"]][1])
                            for entry, meta in zip(approval_data["objects"], receipt["instances"], strict=True)}
    approval_errors = road_approvals(approval_records, approved, transform_key(approval_source))
    if approval_errors and not approvals_native and "layout_version" not in approval_source:
        game_records = {meta["id"]: entry for entry, meta in zip(raw["objects"], receipt["instances"], strict=True)}
        imported = {entry["id"]: entry["game_original"] for entry in approval_source.get("instances", [])
                    if "game_original" in entry}
        remaining = []
        for key in approval_errors:
            record = game_records.get(key)
            snapshot = approved[key]["transform"]
            if record is None or placements[key][1].get("zone") != "road" or record["objectId"] != approved[key]["objectId"]:
                remaining.append(key)
                continue
            if "layout_version" in receipt and key in imported:
                baseline = imported[key]
                current = _layout_transform(baseline, origin)
                snapshot = rebase_legacy_transform(snapshot, baseline, current, origin)
                matches = _approval_matches_map(snapshot, record, origin)
            else:
                matches = _approval_matches_map(snapshot, record, origin, legacy_transform)
            if not matches:
                remaining.append(key)
        approval_errors = remaining
    with MeshLibrary(args.meshes, authoring=use_legacy_frame) as library:
        geo = MapGeometry(placements, library)
        roads = [key for key, (_, meta) in placements.items() if meta.get("zone") == "road" and key in geo.bounds]
        opaque = sorted((set(project.get("opaque_instances", [])) | set(roads)) & geo.bounds.keys())
        supports = [key for key, (entry, meta) in placements.items()
                    if meta.get("role") == "support" or entry["objectId"] == 16180]
        occluders = sorted(set(project.get("opaque_instances", [])) & geo.bounds.keys())
        joins = [join_report(geo, join) for join in project.get("road_joins", [])]
        candidates = []
        for key, (entry, meta) in placements.items():
            if key not in geo.bounds or key in roads:
                continue
            low, high = geo.bounds[key]
            xy = ((low + high) / 2)[:2]
            under = [(road, height - high[2]) for road in roads
                     if (height := geo.top(road, xy)) is not None and height > high[2]]
            if not under:
                continue
            road, clearance = min(under, key=lambda item: item[1])
            row = {"instance": key, "objectId": entry["objectId"], "road": road,
                   "center_projection_clearance_m": float(clearance), "decision": "review",
                   "reason": "below road at center projection"}
            if key in supports:
                row.update(decision="keep", reason="structural support")
            elif entry["deviceIndex"] >= 0 or meta.get("gameplay") is True:
                row.update(decision="keep", reason="gameplay function")
            else:
                row["views"] = route_visibility(geo, key, viewpoints, opaque)
                if any(view["status"] == "visible_sample" for view in row["views"]):
                    row.update(decision="keep", reason="visible from a sampled route point")
                elif viewpoints and meta.get("role") == "decor" and meta.get("gameplay") is False:
                    proof = enclosing_object(geo, [other for other in occluders if other != key], low, high)
                    if proof and str(entry["objectId"]) in weights:
                        row.update(decision="suggest_remove", reason="non-gameplay decor inside closed opaque mesh",
                                   enclosure=proof, saved_weight=weights[str(entry["objectId"])]["weight"])
            candidates.append(row)
    missing_meshes = sorted(set(placements) - geo.bounds.keys())
    strict = args.strict or bool(args.require)
    missing_joins = [{"from": join["from"], "to": join["to"]} for join in joins if "error" in join]
    join_findings = sum(any(sample["findings"] for sample in join.get("samples", ())) for join in joins)
    ran_views = any("views" in row for row in candidates)
    if "viewpoints" in args.require and not ran_views:
        missing_views.extend(view["name"] for view in viewpoints)
    has_annotations = bool(joins or approved or requested_viewpoints)
    report_coverage = coverage.summary(strict,
        instances=coverage.check(len(placements), len(geo.bounds), missing_meshes),
        road_joins=coverage.check(len(joins), len(joins) - len(missing_joins), missing_joins,
                                  findings=join_findings, required="road_joins" in args.require,
                                  requested_item="project.road_joins"),
        viewpoints=coverage.check(len(requested_viewpoints), len(viewpoints) if ran_views else 0, missing_views,
                                   required="viewpoints" in args.require, requested_item="project.viewpoints",
                                   skipped=not ran_views),
        approved_road=coverage.check(len(approved), len(approved), findings=len(approval_errors),
                                      required="approved_road" in args.require,
                                      requested_item="project.approved_road"),
        annotations=coverage.check(int(has_annotations), int(has_annotations), required=strict,
                                    requested_item="project route annotations"),
    )
    report = {"pugc_sha256": receipt["pugc_sha256"], "approved_road_count": len(approved),
              "game_origin_x_cm": receipt["origin_x_cm"], "geometry_check": report_coverage,
              "changed_approved_road": approval_errors, "road_joins": joins,
              "viewpoints": viewpoints, "opaque_instances": sorted(opaque),
              "kept_structural_instances": supports,
              "missing_meshes": missing_meshes,
              "under_road": candidates, "under_road_count": len(candidates),
              "suggested_savings": sum(row.get("saved_weight", 0) for row in candidates)}
    from .ugc_storage import output_paths

    binary = Path(args.pugc)
    (output,) = output_paths((args.out,), inputs=(binary, binary.with_suffix(binary.suffix + ".json"),
                                                args.project, args.meshes, args.profile, args.receipt))
    save_json(output, report)
    if machine:
        diagnostics = [cli.missing_mesh(args.meshes, instance_id=key, object_id=placements[key][0]["objectId"])
                       for key in report["missing_meshes"]]
        diagnostics.extend(
            cli.diagnostic("geometry_mismatch", "The approved road instance changed.",
                           "Compare the reported instance with the approved road placement.",
                           path=args.out, field=f"changed_approved_road[{index}]", instance_id=key)
            for index, key in enumerate(approval_errors)
        )
        for index, join in enumerate(joins):
            if "error" in join:
                diagnostics.append(cli.diagnostic("road_join_unchecked", join["error"],
                                                  "Verify the route's instance IDs and mesh archive.",
                                                  path=args.out, field=f"road_joins[{index}]"))
            elif any(sample["findings"] for sample in join["samples"]):
                diagnostics.append(cli.diagnostic("geometry_mismatch", f"Road join {join['from']} → {join['to']}.",
                                                  "Inspect the lane findings in the audit report.",
                                                  path=args.out, field=f"road_joins[{index}].samples"))
        diagnostics.extend(coverage.partial_diagnostics(report_coverage, args.out))
        return cli.response({"pugc_sha256": receipt["pugc_sha256"], "geometry_check": report_coverage},
                            artifacts={"report": args.out},
                            exit_code=coverage.completion(report_coverage),
                            diagnostics=diagnostics)
    if strict and report_coverage["status"] == "partial":
        print(f"Partial geometry audit: {args.out}", file=sys.stderr)
        sys.exit(cli.RESOURCE)
    if approval_errors or any("error" in join or any(s["findings"] for s in join["samples"]) for join in joins):
        sys.exit(2)


if __name__ == "__main__":
    main()
