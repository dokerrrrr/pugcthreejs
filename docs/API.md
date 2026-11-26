# CLI requests and responses

Put `--json` before the command. Standard output contains one JSON
document with schema 1. The process exit status matches `exit_code`.

| Code | Result |
| --- | --- |
| 0 | Operation completed |
| 1 | Invalid map, project, or request |
| 2 | Discrepancy detected |
| 4 | Resource unavailable or coverage incomplete |
| 5 | Write failure or revision conflict |
| 64 | Invalid CLI arguments |

## Assemble from parts

`search` accepts a purpose, dimensions in meters, a tolerance, a maximum
instance weight, and a result limit. `inspect` accepts `--id`, `--assembly`,
`--zone`, and `--bounds XMIN YMIN ZMIN XMAX YMAX ZMAX` in centimeters.
With `--meshes`, the selection uses native geometry bounds.
`--terrain` also returns ground elevations at the selected pivots.

Use the current project SHA-256 in the request. `seat` sets the Z
coordinate of an existing support. `place_relative` adds an instance
along a signed game axis. A plane at the outermost vertex of each
support determines the distance along that axis. The report also shows
overlap on the cross axis and contact with the support surface. Terrain
elevation comes from the triangles under the mesh's lowest points.

```json
{
  "schema": 1,
  "base_sha256": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "operations": [
    {"op": "seat", "id": "gate-a", "support": "terrain"},
    {"op": "place_relative", "id": "gate-b", "objectId": 12005,
     "anchor_id": "gate-a", "axis": "+X", "gap_cm": 600,
     "support": "terrain", "assembly": "Gate"}
  ]
}
```

`add` and `update` accept `at_cm`, `rotation_xyzw` or `yaw_deg`,
`scale`, and metadata. `delete` takes an ID. `edit --dry-run` returns
`changes` and `result_sha256`. Applying the batch checks the revision
under a lock. Use `scale: [1, 1, 1]` for buildings that players can enter.
`search` and `asset` flag these models with `prefer_native_scale`.
At other scales, `place`, `edit`, `validate`, and `build` return a
`house_scale` warning. A successful operation still has status `ok`.
The Toolkit identifies buildings by model category and name. Override
that classification with `enterable: true` or `enterable: false` in the profile.

## Asset bundle

`fetch-assets` takes one of `--project`, `--ids`, and `--ids-file`.
The ID file can be a JSON array of integers or text separated by whitespace
and commas. `--meshes` and `--textures` specify local ZIPs and override
files from `PUGC_ASSET_DIR`. Use `--bindings` to specify exact texture
paths for individual materials.

`data.models` lists mesh availability and bindings by `objectId`.
For alpha-tested materials, `alpha_mask` points to a PNG from the
original texture's alpha channel and `alpha_test` sets the cutoff in
Three.js. Missing assets produce `coverage: partial` and exit code 4.
When some models can be assembled, the tool saves a new ZIP containing
the report. On a retry, it replaces the existing ZIP once assembly finishes.

## Site and camera

`area XMIN YMIN XMAX YMAX` uses game centimeters. For the island, the
default computes extrema from triangles, including intersections with
the rectangle's boundaries. `--map project.json` or `--map map.pugc`
adds placed meshes from `--meshes`. The available `--surface` modes
are `terrain`, `placements`, and `top`. With a map, the default is
`top`, the highest surface across terrain and placements.
`--surface placements` works at any game XY coordinates. `--ids`
filters by instance ID. `--z-range-cm MIN MAX` limits intersection
elevation. The water level is reported separately as `water_z_cm`.

For placed meshes and requests with `--grid`, `--samples`, or
`--z-range-cm`, the tool samples the highest elevation at the center of
each cell. `--samples` sets the number of cells along the longer side
and defaults to 128. The JSON response includes `min_z_cm`, `max_z_cm`,
`height_range_cm`, and extremum coordinates. For sampling, these values
describe the grid points. `--grid` adds the X and Y axes and a Z matrix
with `null` for empty cells. Code 4 means incomplete coverage or missing
meshes. `--out site.png` adds a height map in `artifacts.top_view` with
X pointing right and Y down.

`preview` accepts an `--eye-cm X Y Z` and `--target-cm X Y Z` pair.
`--fov-deg` sets the vertical field of view and defaults to 50°.
`--roll-deg` sets camera roll. `--clip-cm NEAR FAR` sets the near and
far planes. `--ortho-height-cm` sets the orthographic frame height in
place of FOV. `--size WIDTH HEIGHT` sets resolution and aspect ratio.
`camera_pose` returns reusable settings for `preview --camera-file pose.json`.

`flythrough --path flight.json --out flight.mp4` accepts keyframes with
the same camera fields plus `time_s`. Time starts at zero and increases.
The projection stays consistent throughout the flythrough. `linear`
interpolates parameters between keyframes. `smooth` smooths the position
and aim at joins. The final frame uses the final pose. The MP4 path is
returned in `artifacts.video`.

```json
{
  "fps": 30,
  "interpolation": "smooth",
  "keyframes": [
    {"time_s": 0, "eye_cm": [49700, 50500, 1450], "target_cm": [51000, 51000, 1150],
     "fov_deg": 55, "roll_deg": 0, "clip_cm": [10, 200000]},
    {"time_s": 3, "eye_cm": [51100, 50300, 1700], "target_cm": [51000, 51000, 1150],
     "fov_deg": 45, "roll_deg": 2, "clip_cm": [10, 200000]},
    {"time_s": 6, "eye_cm": [51700, 51900, 2000], "target_cm": [51000, 51000, 1150],
     "fov_deg": 50, "roll_deg": 0, "clip_cm": [10, 200000]}
  ]
}
```

## Example responses

```json
{"schema":1,"status":"ok","exit_code":0,"data":{"gap_cm":600,"tolerance_cm":1,"coverage":"complete","fits":true},"artifacts":{"report":"/work/measurement.json"},"diagnostics":[]}
```

```json
{"schema":1,"status":"error","exit_code":5,"data":{},"artifacts":{},"diagnostics":[{"code":"revision_conflict","message":"Project SHA-256 changed.","action":"Read the current project and prepare a fresh request.","severity":"error","path":"/work/project.json","field":"base_sha256","instance_id":null,"object_id":null}]}
```

[JSON Schema for projects, receipts, batch edits, and responses](schemas/game-space-v2.schema.json)
