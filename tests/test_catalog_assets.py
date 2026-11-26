import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CatalogCliTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, "-B", "-m", "pugc_toolkit", "--json", *args],
                                cwd=self.work, env={**os.environ, "PYTHONPATH": str(ROOT)},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return json.loads(result.stdout)

    def test_aliases_and_different_empty_search_states(self):
        wall = self.cli("search", "wall", "--limit", "2")["data"]
        self.assertEqual(wall, self.cli("search", "стена", "--limit", "2")["data"])
        self.assertTrue(wall["results"])
        self.assertTrue(all(row["category"] == "Wall" for row in wall["results"]))
        self.assertEqual(wall["catalog_coverage"]["index_only_ids"], [24])
        blank = self.cli("search", "", "--limit", "2")["data"]
        self.assertEqual(blank["query_status"], "empty")
        self.assertNotEqual(blank["results"], wall["results"])
        found = self.cli("search", "nonexistentmodel")["data"]
        self.assertEqual((found["query_status"], found["results"]), ("no_matches", []))
        unknown = self.cli("search", "несуществующий")
        self.assertEqual(unknown["data"]["query_status"], "unsupported_term")
        self.assertEqual(unknown["data"]["results"], [])
        self.assertIn("Wall", unknown["diagnostics"][0]["action"])

    def test_exact_id_category_and_dimensions(self):
        card = self.cli("search", "010247")["data"]["results"]
        self.assertEqual([item["objectId"] for item in card], [10247])
        house = card[0]
        self.assertEqual(house["category"], "SmallHouse")
        self.assertIn("DoorFrame", house["first_mesh_name"])
        self.assertTrue(house["profile_revision"])
        self.assertEqual(house["placement_status"], "listed")
        self.assertEqual(house["dimensions_source"], "catalog_sample")
        self.assertEqual(self.cli("search", "SmallHouse", "--limit", "1")["data"]["results"][0]["category"],
                         "SmallHouse")
        size = [str(value) for value in house["dimensions_m"]]
        measured = self.cli("search", "10247", "--dimensions", *size, "--tolerance", "0")["data"]["results"]
        self.assertEqual(measured, card)
        missing = self.cli("search", "10247", "--dimensions", "1", "1", "1", "--tolerance", "0.1")["data"]
        self.assertEqual(missing["query_status"], "no_matches")
        for args in (("--limit", "0"), ("--limit", "-1"), ("--dimensions", "nan", "1", "1"),
                     ("--dimensions", "0", "1", "1"), ("--tolerance", "1")):
            with self.subTest(args=args):
                self.assertEqual(self.cli("search", "wall", *args, code=64)["diagnostics"][0]["code"],
                                 "invalid_arguments")

    def test_profile_exception_and_material_evidence(self):
        apple = self.cli("asset", "24")["data"]
        self.assertTrue(apple["geometry_indexed"])
        self.assertIsNone(apple["mesh_available"])
        self.assertIsNone(apple["weight"])
        self.assertEqual(apple["placement_status"], "not_in_profile")
        wall = self.cli("asset", "12005")["data"]
        self.assertEqual(wall["thumbnail_source"], "editor_catalog")
        material = wall["materials"][0]
        self.assertNotEqual(material["binding"], "confirmed")
        bindings = self.work / "bindings.json"
        bindings.write_text(json.dumps({material["material"]: "chosen.webp"}))
        displayed = self.cli("asset", "12005", "--bindings", bindings)["data"]["materials"][0]
        self.assertEqual((displayed["texture"], displayed["binding"], displayed["confidence"]),
                         ("chosen.webp", "confirmed", None))
