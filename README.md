# PUGC Three.js Toolkit

A local toolkit for creating, editing, and verifying static PUBG UGC maps.
Python preserves native `spawnTransform` values and writes PUGC v3 files.
Three.js opens drafts and saved maps. [Original Toolkit 1.0.1](https://github.com/sashakykyshka17-pixel/test-private-repository/tree/a660066f7133edaea76805338c05bec4d72cfd5b)

## Installation

Python 3.11 or later and OpenSSL are required. Building the browser renderer
from source requires Node.js 22 or later. The wheel includes the compiled renderer.

```sh
npm --prefix web ci
npm --prefix web run build
python -m pip install '.[render]'
python -m playwright install chromium
```

To use an existing Chromium binary with rendering commands, pass
`--chromium /path/to/chromium`. A basic installation with
`python -m pip install .` supports the core commands. Selective asset
extraction and measurements use `.[geometry]`. MP4 output requires FFmpeg
with libx264. `pugc --json doctor --render` launches Chromium through
Playwright and checks WebGL 2. To test a system browser, specify its path.

```sh
pugc --json doctor --chromium /usr/bin/chromium
```

## Build and verify your first map

```sh
export PUGC_ASSET_DIR=/path/to/pugc-assets
mkdir -p "$PWD/work"
pugc fetch-pack small --out "$PWD/work/route-meshes.zip"
pugc --json doctor --mode small --cache-dir "$PWD/work" --island
python examples/ugc_route/demo.py gate --work "$PWD/work/gate" --meshes "$PWD/work/route-meshes.zip"
python examples/ugc_route/demo.py tower --work "$PWD/work/tower" --meshes "$PWD/work/route-meshes.zip"
```

The gate directory will contain `gate.pugc`, `gate.pugc.json`,
`measurement.json`, `gate.png`, and `trace.json`. The tower correction
demo writes the corrected map and sidecar, paired frames from the
`Approach` and `Alongside` cameras, and comparison reports. Add
`--no-render` to run without Chromium.

Put verified copies of `mod_main_level.glb` and `route-meshes.zip` in
`PUGC_ASSET_DIR`. An existing island cache in `$XDG_CACHE_HOME/pugc-toolkit`
or `~/.cache/pugc-toolkit` also works. Use `--base-level` to select a
specific GLB. `fetch-pack small` can also extract the nine required
meshes from `ugc_meshes_all.zip` in that directory or a ZIP specified
with `--archive`. The Toolkit verifies the island and mesh checksums.

## Agent CLI

```sh
pugc --json capabilities
pugc init project.json --name "New Map"
pugc --json search wall --dimensions .6 .6 3 --tolerance .35 --max-weight 1 --limit 8
pugc place project.json gate-a 12005 --at-cm 50600 51000 0 --yaw-deg 0 --assembly Gate
pugc --json inspect project.json --id gate-a
pugc --json edit project.json request.json --dry-run --meshes /path/to/route-meshes.zip
pugc --json edit project.json request.json --meshes /path/to/route-meshes.zip
pugc build project.json map.pugc
pugc --json validate map.pugc
pugc --json measure-gap map.pugc gate-a gate-b --expected-cm 600 --support terrain --meshes /path/to/route-meshes.zip
```

Schema 2 projects and receipts use `coordinate_space: game-xyz-cm`.
`pugc import` preserves game-record order and accepts a 1.x or 2.x receipt
or no receipt. `pugc migrate old.json project.json` upgrades an older
project and preserves the coordinate system of its annotations. Validate
a project before building with `pugc --json validate --project project.json`.
Road placement operations account for snapshots from `approve-road`.

[Operation contract and example responses](docs/API.md) · [Three.js scene manifest](docs/SCENE.md)

## Client save version

New projects use `buildVersion` `2611.1.2`. If a newer client save uses a different version, pass the path to any compatible v3 map.

```sh
pugc init project.json --name "New Map" --version-from latest.pugc
pugc build project.json map.pugc
```

`--version-from` copies only `buildVersion`. The project stores it in its JSON. For an existing project, `pugc build project.json map.pugc --version-from latest.pugc` sets the version for that export. A normal import and build preserve the source map's version. To copy game settings and owner metadata, initialize the project with `--template` and a save from your client.

Each v3 map derives its key from the length and checksum of its payload. The build version lives inside that payload. A client change to the container format or encryption requires a codec update.

Validation warns when positive scales fall outside the authoring profile. `pugc validate map.pugc --strict-scale` treats those limits as errors.

## Map assets

To get the full asset pack, `pugc fetch-pack full --checkout DIR --cache-dir "$PUGC_ASSET_DIR"`
requests missing mesh and texture ZIPs in one Git LFS batch.

```sh
pugc --json fetch-assets --project project.json --out work/map-assets.zip
pugc --json fetch-assets --ids 12005 16052 10002 --out work/selected-assets.zip
```

`fetch-assets` also accepts a saved `.pugc` through `--project` or a list
of IDs through `--ids-file`. Pass local ZIPs through `--meshes` and
`--textures`, or put `ugc_meshes_all.zip` and `PUGC_Textures_Ready.zip`
in `PUGC_ASSET_DIR`. The result contains meshes, textures, masks derived
from alpha channels, and a material manifest. To render it, pass it
through `--meshes` with `--mode textured`. The same ZIP can be passed
to `fetch-assets --meshes` to extract a subset of textured assets.

## Site and camera

```sh
pugc --json area 50555 50832 51113 51328 --map project.json \
  --meshes work/map-assets.zip --out work/site.png
pugc --json preview map.pugc --meshes work/map-assets.zip --mode textured \
  --eye-cm 49700 50500 1400 --target-cm 51000 51000 1150 --fov-deg 50 \
  --roll-deg 2 --clip-cm 10 200000 --out work/approach.png
pugc --json flythrough map.pugc --meshes work/map-assets.zip --mode textured \
  --path flight.json --size 1920 1080 --out work/flight.mp4
```

`area` takes `XMIN YMIN XMAX YMAX` in game centimeters. For a site on a
road or platform, use `--surface placements` and, if needed, `--ids`
and `--z-range-cm`. `--grid` includes heights in the JSON.
`preview --camera-file` reads camera settings from JSON. `flythrough`
uses the same camera format in each keyframe.

[Site and camera path formats](docs/API.md#site-and-camera) · [Acceptance results and benchmarks](docs/ACCEPTANCE.md)
