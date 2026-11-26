# Scene manifest v1

`kind` is `draft`, `build`, or `reference`. For drafts,
`project_sha256` hashes the project bytes. For builds, `pugc_sha256`
hashes the binary that Python read and matched to the receipt. `id`
identifies the placement and `objectId` identifies the model.
`spawnTransform` remains in game space.

| Field | Game → Three.js | Three.js → game |
| --- | --- | --- |
| Position | `(x/100, z/100, y/100)` | `(100x, 100z, 100y)` |
| XYZW quaternion | `(−qx, −qz, −qy, qw)` | `(−qx, −qz, −qy, qw)` |
| Scale | `(sx, sz, sy)` | `(sx, sz, sy)` |

Game Z and view Y point up. A +90° game rotation moves local +X toward
+Y and appears as a −90° rotation around Three.js Y.
[pugc-editor transforms](https://github.com/PlzDntKilMeh/pugc-editor/blob/3bd69a10c3ad9ebe8f9aa44964a36784b7bce29b/scene-editor/coords.js#L30-L67)

`bp-mesh` vertices, normals, and local matrices already use view axes.
Part matrices apply in the same basis. A negative matrix determinant
reverses the corner order and corresponding UVs together. The renderer
preserves the native pivot. The terrain uses the same axes, with water
at Y = 0.

Placement matrices are row-major and operate on column vectors. The
binary asset contains, in order, `vertices × 3` float32 values,
`triangles × 3` uint32 values, `triangles × 3 × 2` float32 values,
and, when `normals: true`, `vertices × 3` float32 values. All numbers
are little-endian. UVs follow triangle corner order. The manifest
specifies array sizes, SHA-256, and native material ranges.

In textured mode, a model can include `materials` with the binding
source and texture URL. The renderer applies `alpha_test` thresholds
to the original texture's alpha channel.

Cameras use local OpenGL axes and a six-plane frustum
`[left, right, bottom, top, near, far]`. For a legacy reference NPZ,
`source_to_authoring_m` applies to geometry and cameras before
conversion to view space. The map codec reads and writes only game
`spawnTransform` values.
