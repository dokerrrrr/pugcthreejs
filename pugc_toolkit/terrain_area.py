"""Measure a rectangle on the island or on placed map geometry."""
from io import BytesIO
from math import isfinite

import numpy as np

from . import cli_protocol as cli
from .base_level import WATER_Z_M
from .game_space import SPACE, TRANSFORM
from .placement_geometry import _terrain_mesh, projected_heights, world_vertices
from .scene import file_sha256


def terrain_bounds(bounds):
    x0, y0, x1, y1 = map(float, bounds)
    area = (x1 - x0) * (y1 - y0)
    if not all(map(isfinite, (x0, y0, x1, y1, area))) or x0 >= x1 or y0 >= y1 or area <= 0:
        raise ValueError("Expected a rectangle in centimetres ordered as XMIN YMIN XMAX YMAX")
    return x0, y0, x1, y1


def _area(polygons):
    # A fan at the first vertex measures in XY and avoids subtracting large world coordinates.
    edges = polygons[..., 1:, :2] - polygons[..., :1, :2]
    a, b = edges[..., :-1, :], edges[..., 1:, :]
    return np.abs((a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]).sum(axis=-1)) / 2


def _clip(triangle, bounds):
    polygon = list(triangle)
    for axis, edge, sign in zip((0, 1, 0, 1), bounds, (1, 1, -1, -1), strict=True):
        if not polygon:
            break
        previous = polygon[-1]
        distance = sign * (previous[axis] - edge)
        clipped = []
        for point in polygon:
            next_distance = sign * (point[axis] - edge)
            if (distance >= 0) != (next_distance >= 0):
                cross = previous + (point - previous) * (distance / (distance - next_distance))
                cross[axis] = edge
                clipped.append(cross)
            if next_distance >= 0:
                clipped.append(point)
            previous, distance = point, next_distance
        polygon = clipped
    return np.asarray(polygon).reshape(-1, 3)


def terrain_area(path, bounds, *, top_view=False):
    bounds = terrain_bounds(bounds)
    lower, upper = np.asarray(bounds).reshape(2, 2)
    digest = file_sha256(path)
    extrema, visible = [], []
    covered = 0.
    for triangles, low, high in _terrain_mesh(path, digest):
        if np.any(high.max(0) <= lower) or np.any(low.min(0) >= upper):
            continue
        overlaps = np.all((high > lower) & (low < upper), axis=1)
        candidates = triangles[overlaps]
        if not len(candidates):
            continue
        if top_view:
            visible.append(candidates)
        contained = np.all((low[overlaps] >= lower) & (high[overlaps] <= upper), axis=1)
        inside = candidates[contained]
        areas = _area(inside)
        covered += areas.sum()
        vertices = inside[areas > 0].reshape(-1, 3)
        if len(vertices):
            extrema.extend((vertices[vertices[:, 2].argmin()], vertices[vertices[:, 2].argmax()]))
        for triangle in candidates[~contained]:
            polygon = _clip(triangle, bounds)
            area = _area(polygon)
            if area > 0:
                covered += area
                extrema.extend((polygon[polygon[:, 2].argmin()], polygon[polygon[:, 2].argmax()]))
    fraction = float(min(1, covered / np.prod(upper - lower)))
    coverage = "complete" if fraction >= 1 - 1e-9 else "partial" if extrema else "none"
    data = {"coordinate_space": SPACE, "bounds_cm": list(bounds), "base_level_sha256": digest,
            "coverage": coverage, "coverage_fraction": 1. if coverage == "complete" else fraction,
            "water_z_cm": WATER_Z_M * 100, "min_z_cm": None, "max_z_cm": None, "height_range_cm": None,
            "min_at_cm": None, "max_at_cm": None}
    if not extrema:
        return data, None
    points = np.asarray(extrema)
    a, b = points[points[:, 2].argmin()], points[points[:, 2].argmax()]
    data.update(min_z_cm=float(a[2]), max_z_cm=float(b[2]), height_range_cm=float(b[2] - a[2]),
                min_at_cm=a[:2].tolist(), max_at_cm=b[:2].tolist())
    return data, _top_view(np.concatenate(visible), data) if top_view else None


def _height_image(triangles, bounds, width, height):
    # The pinned island GLB has two triangles per regular grid cell, including along tile seams.
    points = triangles[:, :, :2]
    low, high = points.min(axis=1), points.max(axis=1)
    origin, limit = low.min(0), high.max(0)
    step = high[0] - low[0]
    nx, ny = np.rint((limit - origin) / step).astype(np.intp)
    columns, rows = np.rint((low - origin) / step).astype(np.intp).T
    cells = rows * nx + columns
    order = np.argsort(cells, kind="stable")
    keys, counts = np.unique(cells[order], return_counts=True)
    if not np.all(counts == 2):
        raise ValueError("The island terrain must have two triangles per grid cell")
    index = np.full((nx * ny, 2), -1, dtype=np.intp)
    index[keys] = order.reshape(-1, 2)

    x, y = _sample_axes(bounds, width, height)
    xy = np.stack(np.meshgrid(x, y), axis=-1)
    hits = np.all((xy >= origin) & (xy < limit), axis=-1)
    pixels = xy[hits]
    col, row = np.floor((pixels - origin) / step).astype(np.intp).T
    faces = index[row * nx + col]
    z = np.full((len(pixels), 2), np.nan)
    for side in range(2):
        available = faces[:, side] >= 0
        z[available, side] = projected_heights(triangles[faces[available, side]], pixels[available])
    values = np.full((height, width), np.nan)
    values[hits] = np.fmax(z[:, 0], z[:, 1])
    return values


def _top_view(triangles, data):
    width, height = _dimensions(data["bounds_cm"], 640)
    return _paint(_height_image(triangles, data["bounds_cm"], width, height), data)


def _dimensions(bounds, samples):
    dx, dy = bounds[2] - bounds[0], bounds[3] - bounds[1]
    scale = max(dx, dy)
    return tuple(max(1, round(samples * (span / scale))) for span in (dx, dy))


def _sample_axes(bounds, width, height):
    return tuple(start + (np.arange(count) + .5) * ((end - start) / count)
                 for start, end, count in zip(bounds[:2], bounds[2:], (width, height), strict=True))


def _paint(values, data):
    from PIL import Image, ImageDraw

    bounds = data["bounds_cm"]
    height, width = values.shape
    low, high, spread = (data[key] for key in ("min_z_cm", "max_z_cm", "height_range_cm"))
    covered = np.isfinite(values)
    shade = np.where(covered, (values - low) / spread if spread else .5, 0)
    palette = ((38, 51, 99), (44, 113, 147), (64, 153, 119), (175, 195, 99), (255, 225, 124))
    neutral, paper, ink = (220, 224, 230), (249, 250, 251), (29, 37, 49)
    stops = np.linspace(0, 1, len(palette))
    rgb = np.stack([np.interp(shade, stops, channel) for channel in zip(*palette)], axis=-1).astype(np.uint8)
    rgb[~covered] = neutral
    canvas = Image.new("RGB", (max(448, width + 48), height + 108), paper)
    left, top = (canvas.width - width) // 2, 35
    canvas.paste(Image.fromarray(rgb), (left, top))
    draw = ImageDraw.Draw(canvas)
    draw.rectangle((left - 1, top - 1, left + width, top + height), outline=ink)
    draw.text((20, 11), f"X {bounds[0]:g} to {bounds[2]:g}    Y {bounds[1]:g} to {bounds[3]:g} cm", fill=ink)
    y = top + height + 15
    bar_width = 160
    for i in range(bar_width):
        color = tuple(round(np.interp(i / (bar_width - 1), stops, channel)) for channel in zip(*palette))
        draw.line((20 + i, y, 20 + i, y + 11), fill=color)
    draw.text((190, y - 1), f"Z {low:.2f} to {high:.2f} cm", fill=ink)
    draw.text((20, y + 29), "X increases right, Y increases down", fill=ink)
    if data["coverage"] != "complete":
        draw.rectangle((280, y + 29, 292, y + 40), fill=neutral)
        draw.text((300, y + 29), "No data", fill=ink)
    png = BytesIO()
    canvas.save(png, format="PNG")
    return png.getvalue()


def _surface_heights(values, triangles, x, y, z_range):
    low, high = triangles.min(1), triangles.max(1)
    x0, y0 = np.searchsorted(x, low[:, 0]), np.searchsorted(y, low[:, 1])
    x1, y1 = np.searchsorted(x, high[:, 0], side="right"), np.searchsorted(y, high[:, 1], side="right")
    covered = (x0 < x1) & (y0 < y1) & (low[:, 2] <= z_range[1]) & (high[:, 2] >= z_range[0])
    single = covered & (x1 - x0 == 1) & (y1 - y0 == 1)
    cols, rows = x0[single], y0[single]
    z = projected_heights(triangles[single], np.column_stack((x[cols], y[rows])))
    np.fmax.at(values, (rows, cols), np.where((z >= z_range[0]) & (z <= z_range[1]), z, np.nan))
    for face in np.flatnonzero(covered & ~single):
        xy = np.column_stack([axis.ravel() for axis in np.meshgrid(x[x0[face]:x1[face]], y[y0[face]:y1[face]])])
        z = projected_heights(triangles[face:face + 1], xy)
        patch = values[y0[face]:y1[face], x0[face]:x1[face]]
        np.fmax(patch, np.where((z >= z_range[0]) & (z <= z_range[1]), z, np.nan).reshape(patch.shape), out=patch)


def _map_surfaces(path, meshes, receipt, ids, values, x, y, z_range):
    from .inspect_project import read_project
    from .mesh_library import MeshLibrary
    from .ugc_project import _game_records, raw_with_receipt

    if path.suffix.lower() == ".pugc":
        raw, metadata = raw_with_receipt(path, receipt)
        labels = metadata["instances"]
        hashes = {"pugc_sha256": metadata["pugc_sha256"]}
    else:
        project, revision = read_project(path)
        raw, labels = _game_records(project), project["instances"]
        hashes = {"project_sha256": revision}
    if ids is not None and (unknown := set(ids) - {row["id"] for row in labels}):
        raise ValueError(f"Unknown placement IDs: {sorted(unknown)}")
    cli.archive_input(meshes, "--meshes", machine=True)
    issues = []
    with MeshLibrary(meshes) as library:
        for row, record in zip(labels, raw["objects"], strict=True):
            if ids is not None and row["id"] not in ids:
                continue
            oid = record["objectId"]
            if oid not in library.ids:
                issues.append(cli.missing_mesh(meshes, instance_id=row["id"], object_id=oid))
                continue
            mesh = library.geometry(oid)
            vertices = world_vertices(mesh, record[TRANSFORM])
            if not len(mesh.triangles):
                issues.append(cli.diagnostic("mesh_empty", "Model has no triangles.", "Supply a renderable mesh.",
                                             path=meshes, instance_id=row["id"], object_id=oid))
                continue
            low, high = vertices.min(0), vertices.max(0)
            if low[0] <= x[-1] and high[0] >= x[0] and low[1] <= y[-1] and high[1] >= y[0]:
                _surface_heights(values, vertices[mesh.triangles], x, y, z_range)
    return hashes, issues


def sampled_area(bounds, *, island, source, meshes, receipt, ids, samples, z_range, surface, top_view, grid):
    width, height = _dimensions(bounds, samples)
    x, y = _sample_axes(bounds, width, height)
    values = np.full((height, width), np.nan)
    hashes, issues = {}, []
    if island is not None:
        digest = file_sha256(island)
        hashes["base_level_sha256"] = digest
        lower, upper = np.asarray(bounds).reshape(2, 2)
        parts = [triangles[np.all((high > lower) & (low < upper), axis=1)]
                 for triangles, low, high in _terrain_mesh(island, digest)]
        if any(len(part) for part in parts):
            ground = _height_image(np.concatenate(parts), bounds, width, height)
            values = np.where((ground >= z_range[0]) & (ground <= z_range[1]), ground, np.nan)
    if source is not None:
        map_hash, issues = _map_surfaces(source, meshes, receipt, ids, values, x, y, z_range)
        hashes.update(map_hash)
    found = int(np.count_nonzero(np.isfinite(values)))
    coverage = "complete" if found == values.size and not issues else "partial" if found or issues else "none"
    data = {"coordinate_space": SPACE, "bounds_cm": list(bounds), **hashes, "surface": surface,
            "method": "highest_hit_per_sample", "coverage": coverage, "coverage_fraction": found / values.size,
            "water_z_cm": WATER_Z_M * 100, "samples": found, "grid_size": [width, height],
            "min_z_cm": None, "max_z_cm": None, "min_at_cm": None, "max_at_cm": None, "height_range_cm": None}
    if grid:
        data.update(grid_x_cm=x.tolist(), grid_y_cm=y.tolist(),
                    grid_z_cm=[[float(z) if isfinite(z) else None for z in row] for row in values])
    if not found:
        return data, None, issues
    first, last = (np.unravel_index(fn(values), values.shape) for fn in (np.nanargmin, np.nanargmax))
    low, high = float(values[first]), float(values[last])
    data.update(min_z_cm=low, max_z_cm=high, height_range_cm=high - low,
                min_at_cm=[float(x[first[1]]), float(y[first[0]])], max_at_cm=[float(x[last[1]]), float(y[last[0]])])
    if top_view:
        from PIL import Image

        size = _dimensions(bounds, max(width, height, 480))
        pixels = Image.fromarray(values.astype("float32")).resize(size, Image.Resampling.NEAREST)
        return data, _paint(np.asarray(pixels), data), issues
    return data, None, issues
