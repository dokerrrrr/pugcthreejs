import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / name) for name in ("tools", "examples/ugc_route")]

from prepare import overpass_project, route_project
from ugc_project import MESHES, build, instance, load_json, save_json


class RouteAuditTest(unittest.TestCase):
    def check_project(self, project, expected_code=0):
        archive = Path(os.environ.get("UGC_TEST_MESHES", MESHES))
        if not archive.exists() or archive.stat().st_size < 200:
            self.skipTest("Provide UGC_TEST_MESHES or Git LFS mesh archive")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            pugc, project_json, audit = out / "map.pugc", out / "project.json", out / "audit.json"
            save_json(project_json, project)
            build(project, pugc)
            result = subprocess.run([sys.executable, "-B", "-m", "pugc_toolkit", "--json", "audit", pugc, "--project",
                                     project_json, "--meshes", archive, "--out", audit],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, expected_code, result.stdout + result.stderr)
            return load_json(audit)

    def test_one_hidden_tire_and_two_supports(self):
        project = overpass_project()
        report = self.check_project(project)
        rows = {row["instance"]: row for row in report["under_road"]}
        suggestions = [key for key, row in rows.items() if row["decision"] == "suggest_remove"]
        self.assertEqual(suggestions, ["tire-inside"])
        self.assertEqual(report["suggested_savings"], 1)
        self.assertEqual(report["kept_structural_instances"], ["support-a", "support-b"])
        self.assertIn({"name": "Lower road", "route": "lower", "status": "visible_sample"},
                      rows["tire-visible"]["views"])
        project["instances"].remove(instance(project, "tire-inside"))
        cleaned = self.check_project(project)
        self.assertEqual(cleaned["suggested_savings"], 0)
        self.assertEqual(cleaned["kept_structural_instances"], report["kept_structural_instances"])
        self.assertEqual(report["geometry_check"]["status"], "complete")
        self.assertEqual(report["geometry_check"]["checks"]["viewpoints"]["checked"], 3)
        self.assertEqual(cleaned["geometry_check"]["checks"]["instances"]["checked"], 5)

    def test_directed_join_measures_step_and_overlapping_surfaces(self):
        project = route_project()
        report = self.check_project(project)
        lanes = report["road_joins"][0]["samples"]
        self.assertEqual([row["lane"] for row in lanes], ["left", "center", "right"])
        self.assertTrue(all(abs(row["step_m"]) < .001 for row in lanes))
        project.pop("approved_road")
        t = instance(project, "road-after")["transform"]["translation"]
        t["y"] -= 30
        overlap = self.check_project(project, expected_code=2)
        lanes = overlap["road_joins"][0]["samples"]
        self.assertTrue(all(row["overlap_m"] > .2 and "coplanar_or_intersecting_surface_risk" in row["findings"]
                            for row in lanes))
        t["z"] += 18
        raised = self.check_project(project, expected_code=2)
        for row in raised["road_joins"][0]["samples"]:
            self.assertIn("height_step", row["findings"])
            self.assertAlmostEqual(row["step_m"], .18, places=4)


if __name__ == "__main__":
    unittest.main()
