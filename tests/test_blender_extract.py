"""Integration test, given the NPZ emitted for the example .blend scene."""
import json
import os
from pathlib import Path
import sys
import unittest

import numpy as np


GEOMETRY = os.getenv("UGC_TEST_SOURCE_NPZ")


class BlenderExtraction(unittest.TestCase):
    def test_evaluated_geometry(self):
        if GEOMETRY is None:
            self.skipTest("Set UGC_TEST_SOURCE_NPZ to the extracted gate scene")
        with np.load(GEOMETRY) as data:
            meta = json.loads(data["metadata"].item())
            objects = meta["objects"]
            facade = next(item for item in objects if item["name"] == "Array facade")
            self.assertEqual(facade["triangle_count"], 36)
            self.assertEqual(facade["vertex_count"], 24)
            self.assertEqual(meta["scenes"][0]["units"]["system"], "METRIC")
            self.assertEqual(meta["scenes"][0]["units"]["meters_per_unit"], 1)
            bench_parts = [item for item in objects if "Bench prototype" in item["collections"]]
            self.assertEqual(len(bench_parts), 6)
            self.assertEqual({item["instancer"] for item in bench_parts}, {"Bench west", "Bench east"})
            self.assertTrue(all(item["instance"] for item in bench_parts))
            for item in bench_parts:
                expected_x = -5.8 if item["instancer"] == "Bench west" else 5.8
                self.assertAlmostEqual(item["matrix_world"][0][3], expected_x, places=5)
            tower = next(item for item in objects if item["name"] == "Tower")
            road = next(item for item in objects if item["name"] == "Road")
            self.assertLess(tower["matrix_world"][0][3], road["matrix_world"][0][3])
            camera = next(item for item in meta["cameras"] if item["name"] == "Approach")
            self.assertEqual(camera["resolution"], [720, 405])
            self.assertAlmostEqual(camera["matrix_world"][1][3], -8)
            self.assertEqual(len(data["triangles"]), len(data["materials"]))
            for index, item in enumerate(objects):
                segment = slice(item["triangle_start"], item["triangle_start"] + item["triangle_count"])
                self.assertTrue(np.all(data["triangle_objects"][segment] == index))
                self.assertTrue(np.all(np.isin(data["materials"][segment], item["material_slots"])))


if __name__ == "__main__":
    GEOMETRY = Path(sys.argv.pop(1))
    unittest.main()
