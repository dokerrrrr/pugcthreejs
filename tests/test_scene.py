from hashlib import sha256
import gzip
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

import numpy as np

from pugc_toolkit.pugc_codec import read_game
from pugc_toolkit.scene import game_camera, map_scene
from pugc_toolkit.ugc_geometry import apply_matrix
from pugc_toolkit.game_space import transform_cm, view_placement_matrix
from pugc_toolkit.ugc_project import build, new_game_project, place


class SceneTest(unittest.TestCase):
    def test_orthographic_camera_roll_and_clip_planes(self):
        from pugc_toolkit.camera_motion import camera_record

        camera, _ = camera_record({"eye_cm": [0, 0, 1000], "target_cm": [0, 0, 0],
                                   "ortho_height_cm": 400, "roll_deg": 90, "clip_cm": [10, 2000]}, (400, 200))
        self.assertEqual(camera["type"], "ORTHO")
        np.testing.assert_allclose(camera["planes"], [-4, 4, -2, 2, .1, 20])
        np.testing.assert_allclose(np.asarray(camera["matrix"])[:3, 0], [0, 0, -1], atol=1e-12)

    def test_game_camera_targets_game_coordinates_at_the_image_centre(self):
        target = np.array([5, 1, 10, 1])
        for position in ((600, 800, 300), (500, 1000, 500)):
            frame = game_camera(position, (500, 1000, 100), 60, (1200, 600))
            x, y, z, _ = np.linalg.solve(frame["matrix"], target)
            np.testing.assert_allclose([x, y], [0, 0], atol=1e-12)
            self.assertLess(z, 0)
            self.assertAlmostEqual(frame["planes"][1] / frame["planes"][3], 2)

    def test_asymmetric_mesh_pivot_winding_uv_and_world_coordinates(self):
        vertices = [1, 2, 3, 4, 2, 2, 2, 7, 3]
        uv = [0.1, 0.2, 0.4, 0.8, 0.3, 0.9]
        native = (struct.pack("<III", 0xFFFFFFFE, 1, 3)
                  + struct.pack("<24f", *vertices, *([0] * 9), *uv)
                  + struct.pack("<III", 0, 1, 0)
                  + np.eye(4, dtype="<f4").tobytes())
        project = new_game_project("Asymmetric")
        place(project, "asymmetric", 12005, transform_cm((100, 200, 300), 90, (2, 3, 4)), "A", "near")
        with TemporaryDirectory() as directory:
            path = Path(directory) / "map.pugc"
            meshes = path.with_name("meshes.zip")
            with ZipFile(meshes, "w") as archive:
                archive.writestr("bp-mesh/12005.bin.gz", gzip.compress(native))
            build(project, path)
            self.assertEqual(read_game(path)["objects"][0]["spawnTransform"]["translation"], {"x": 100, "y": 200, "z": 300})
            manifest = map_scene(path, meshes)
            self.assertEqual(manifest.manifest["pugc_sha256"], sha256(path.read_bytes()).hexdigest())
        item, = manifest.manifest["instances"]
        resource = manifest.models[item["model"]]
        self.assertEqual(item["id"], "asymmetric")
        self.assertEqual(resource.triangles.tolist(), [[0, 1, 2]])
        np.testing.assert_allclose(np.frombuffer(resource.data[48:72], dtype="<f4"), uv)
        np.testing.assert_allclose(resource.vertices.tolist(), [[1, 2, 3], [4, 2, 2], [2, 7, 3]])
        np.testing.assert_allclose(apply_matrix(resource.vertices, np.asarray(item["matrix"])),
                                   [[-8, 11, 4], [-5, 11, 10], [-8, 31, 6]], atol=1e-8)

    def test_view_matrix_matches_game_rotation_for_all_basis_vectors(self):
        for quaternion in [(0, 0, 0, 1), (0, 0, .7071067811865476, .7071067811865476),
                           (0, 0, -.7071067811865476, .7071067811865476), (.5, -.5, .5, .5)]:
            with self.subTest(quaternion=quaternion):
                t = transform_cm((100, 200, 300), scale=(2, 3, 4))
                t["rotation"] = dict(zip("xyzw", quaternion))
                x, y, z, w = quaternion
                # Rotate a vector with q*v*q^-1 in game space, then convert the result.
                game_expected = []
                for vector in np.eye(3) * [2, 3, 4]:
                    doubled = 2 * np.cross([x, y, z], vector)
                    world = vector + w * doubled + np.cross([x, y, z], doubled)
                    game_expected.append(world[[0, 2, 1]] + [1, 3, 2])
                np.testing.assert_allclose(
                    apply_matrix(np.eye(3)[:, [0, 2, 1]], view_placement_matrix(t)), game_expected, atol=1e-12)
