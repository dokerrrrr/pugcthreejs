# Working with PUBG UGC maps

**Explicit requests required:** Do not add gameplay devices or configure map settings unless the user explicitly requests that specific action. A general request to create, edit, or improve a map does not authorize either action. Preserve existing devices and map settings unless the user explicitly asks to change them.

- Use `pugc init` for a new map and `pugc import` for an existing map.
  Use game XYZ in centimeters, with Z up, and the model's native pivot.
  A positive `yaw_deg` rotates +X toward +Y.
- Find a native `objectId` with `pugc search`. The placement ID identifies
  which instance of a shared model to edit.
- Use scale `[1, 1, 1]` for houses and other buildings with accessible
  interiors so doors, floors, and stairs keep their dimensions.
  `house_scale` flags a deviation. Make a deliberate exception only
  after checking the entrances and interior routes.
- Inspect the site with `pugc --json area` before placing objects.
  For a road, deck, or other placed surface, provide `--map`, `--meshes`,
  and the appropriate `--ids` or `--z-range-cm`.
- Read `project_sha256` with `pugc --json inspect`. Check the batch with
  `edit --dry-run` before applying it. On `revision_conflict`, inspect
  the changed instances again and prepare a fresh request.
- Build the map and verify the saved binary. Keep the `.pugc` and
  `.pugc.json` together. Match the measurement and frame by `pugc_sha256`.
- Use native shapes and textures for static maps. The preview includes
  the starting island and water at game Z = 0.
- Use `preview` for final frames and `flythrough` for a camera path
  with keyframes in game centimeters.

[Examples and setup](README.md) · [Scene manifest](docs/SCENE.md)
