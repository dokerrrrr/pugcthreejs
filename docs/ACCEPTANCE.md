# Acceptance for issue #1

The demo created a gate map from the neutral template. Re-reading the
binary measured **600 cm** between the inner face planes along +X and
**0 cm** of terrain clearance within a 1 cm tolerance. Ground elevation
at the control point (51000, 51000) was 1064.0625 cm.

The tower correction took the target position `(100800, 1000, 105)` cm
from the decoded reference. The incorrect map returned code 2 and
`side_changed` for `Approach` and `Alongside`. After a single edit
and build, both cameras passed, every other game record matched the
input, and the binary SHA-256 matched the reference. Removing mesh 16087
from the ZIP made the audit return code 4 with coverage for 4 of 6
instances. A damaged receipt returned code 1.

## Benchmarks

AMD EPYC 9V45, 3 vCPUs, Debian x86-64, Python 3.12.14, OpenSSL 3.5.7,
Three.js r187, Chromium 156.0.8078.79, and WebGL SwiftShader. Each
variant was run three times. First-frame time was measured inside
the browser after the render call and before the PNG write. Camera
motion p95 was calculated from 48 renders per scenario. A cold run
opens the manifest and loads its meshes. A warm run renders another
frame with the same open scene.

| Scenario | First frame, cold/warm median | Frame p95 during motion | Draw calls |
| --- | ---: | ---: | ---: |
| Gate | 222.2 / 1.3 ms | 1.3 ms | 8 |
| Tower | 162.0 / 0.9 ms | 0.7 ms | 8 |

The geometry count dropped to zero after each scene closed. The
texture count returned to one, the renderer's base resource.
In a 3,000-instance project, 100 edits took 7.902 s individually and
0.087 s in a batch. The project was written 100 times and once,
respectively. Both projects and both built binaries matched byte for byte.

| Scenario | Variant | Calls | Bytes | Tokens, median | Time, median |
| --- | --- | ---: | ---: | ---: | ---: |
| Gate | Compact responses | 10 | 12,103 | 4,470 | 5.87 s |
| Gate | With an additional full manifest | 11 | 13,530 | 4,970 | 5.69 s |
| Tower | Compact ID lookup | 9 | 10,546 | 3,779 | 6.41 s |
| Tower | Full manifest in place of ID lookup | 9 | 26,791 | 11,055 | 7.63 s |

Both variants of each scenario performed one build and the same
verification steps. Token counts use `cl100k_base`. The three runs
provide initial measurements. CI thresholds are pending.
[Raw benchmarks](acceptance/benchmark.json) · [Batch edit](acceptance/batch.json)

## Verify in PUBG

Acceptance in the target client is pending. Use `gate.pugc` and
`corrected.pugc` with the SHA-256 values from their respective reports.
Open, save, and reopen both maps in the client. Record the client
build, each file hash, the orientation of the asymmetric object marked
+X, gate passability, collision behavior, and materials. Keep the JSON
receipts with the Toolkit files.
[PUBG client verification procedure](https://github.com/sashakykyshka17-pixel/test-private-repository/blob/a660066f7133edaea76805338c05bec4d72cfd5b/docs/PUBG_CLIENT_ACCEPTANCE_RU.md)

## Run geometry acceptance locally

Install the Toolkit with rendering support, Chromium, and FFmpeg with
libx264. [Installation](../README.md#installation)

Put `mod_main_level.glb` and `route-meshes.zip` in your local asset directory.
From the checkout, run the geometry checks with an output directory
outside the repository.

```sh
export PUGC_ASSET_DIR=/path/to/local-fixtures
python -B tests/three_acceptance.py --work /path/to/results \
  --meshes "$PUGC_ASSET_DIR/route-meshes.zip"
```

GitHub Actions runs unit tests, builds the packages, and checks Chromium
and WebGL.
