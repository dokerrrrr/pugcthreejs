import unittest

import numpy as np

from pugc_toolkit.triangle_index import TriangleIndex


class TriangleIndexTest(unittest.TestCase):
    def test_returns_closest_triangle_even_across_tree_nodes(self):
        levels = list(range(80, 0, -1))
        vertices = [[x, y, z] for z in levels for x, y in ((0, 0), (2, 0), (0, 2))]
        tree = TriangleIndex(vertices, np.arange(len(vertices)).reshape(-1, 3))
        hit, normal, face, distance = tree.ray_cast((.2, .7, 0), (0, 0, 3), 18)
        np.testing.assert_allclose(hit, [.2, .7, 1])
        np.testing.assert_allclose(normal, [0, 0, 1])
        self.assertEqual((face, distance), (79, 1))
        self.assertIsNone(tree.ray_cast((.2, .7, 0), (0, 0, 1), .5)[0])
        point, _, face, distance = tree.find_nearest((1, 1, .6))
        np.testing.assert_allclose(point, [1, 1, 1])
        self.assertEqual(face, 79)
        self.assertAlmostEqual(distance, .4)
