from io import BytesIO
import unittest
from unittest.mock import patch

import numpy as np
from PIL import Image

from pugc_toolkit.terrain_area import _surface_heights, terrain_area


class TerrainAreaTest(unittest.TestCase):
    def setUp(self):
        a, b, c, d = (0, 0, 0), (200, 0, 20), (0, 200, 30), (200, 200, 80)
        triangles = np.asarray([[a, b, c], [b, d, c]], dtype=float)
        xy = triangles[:, :, :2]
        self.enterContext(patch("pugc_toolkit.terrain_area.file_sha256", return_value="fixture"))
        self.enterContext(patch("pugc_toolkit.terrain_area._terrain_mesh",
                                return_value=[(triangles, xy.min(1), xy.max(1))]))

    def test_extrema_inside_faces_are_interpolated_at_rectangle_edges(self):
        result, png = terrain_area("island.glb", (50, 60, 150, 140), top_view=True)
        self.assertEqual(result["coverage"], "complete")
        self.assertAlmostEqual(result["min_z_cm"], 14)
        self.assertAlmostEqual(result["max_z_cm"], 49.5)
        self.assertEqual(result["min_at_cm"], [50, 60])
        self.assertEqual(result["max_at_cm"], [150, 140])
        with Image.open(BytesIO(png)) as image:
            self.assertEqual(image.format, "PNG")
            self.assertGreater(len(image.getcolors(maxcolors=10000)), 5)

    def test_partial_and_missing_coverage(self):
        partial, _ = terrain_area("island.glb", (-50, 0, 50, 100))
        self.assertEqual(partial["coverage"], "partial")
        self.assertAlmostEqual(partial["coverage_fraction"], 0.5)
        self.assertAlmostEqual(partial["max_z_cm"], 20)
        missing, image = terrain_area("island.glb", (-100, 0, 0, 100), top_view=True)
        self.assertEqual(missing["coverage"], "none")
        self.assertIsNone(missing["min_z_cm"])
        self.assertIsNone(image)

    def test_placed_surfaces_choose_highest_hit_within_requested_elevation(self):
        lower = np.asarray([[0, 0, 50], [200, 0, 50], [0, 200, 50]], dtype=float)
        triangles = np.stack([lower, lower + [0, 0, 175]])
        x = y = np.asarray([25., 75., 125.])
        values = np.full((len(y), len(x)), np.nan)
        _surface_heights(values, triangles, x, y, (0, 300))
        self.assertEqual(values[0, 0], 225)
        self.assertTrue(np.isnan(values[-1, -1]))
        values.fill(np.nan)
        _surface_heights(values, triangles, x, y, (0, 100))
        self.assertEqual(values[0, 0], 50)
