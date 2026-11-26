"""Exercise the JSON interface through the same subprocess entry point agents use."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


class MachineCliTest(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix="Проверка CLI ")
        self.addCleanup(workspace.cleanup)
        self.work = Path(workspace.name)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT)}

    def cli(self, *args, machine=True):
        prefix = ["--json"] if machine else []
        return subprocess.run([sys.executable, "-B", "-m", "pugc_toolkit", *prefix, *args],
                              cwd=self.work, env=self.env, capture_output=True, text=True, timeout=90)

    def machine(self, *args, code=0, reason=None):
        process = self.cli(*args)
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, code, (result, process.stderr))
        self.assertEqual(result["exit_code"], code)
        self.assertEqual(result["schema"], 1)
        self.assertEqual(result["status"], "error" if code else "ok")
        if reason:
            self.assertEqual(result["diagnostics"][0]["code"], reason)
            self.assertTrue(result["diagnostics"][0]["action"])
        return result

    def test_discovery_and_usage(self):
        capabilities = self.machine("capabilities")["data"]
        self.assertIn("validate", capabilities["commands"])
        self.assertIn("compare", capabilities["blender_operations"])
        self.assertEqual(self.machine("--version")["data"]["version"], capabilities["version"])
        self.assertIn("--json", self.machine("--help")["data"]["help"])
        self.assertIn("metres", self.machine("place", "--help")["data"]["help"])
        self.machine("init", "project.json", code=64, reason="invalid_arguments")
        self.machine("validate", code=64, reason="invalid_arguments")
        legacy = self.cli("search", "wall", "--limit", "1", machine=False)
        self.assertEqual(legacy.returncode, 0)
        self.assertIsInstance(json.loads(legacy.stdout), list)
        self.assertIsInstance(self.machine("search", "wall", "--limit", "1")["data"]["results"], list)

    def test_catalog_and_archive_diagnostics(self):
        asset = self.machine("asset", "100000000", code=4, reason="asset_unknown")["diagnostics"][0]
        self.assertEqual(asset["object_id"], 100000000)
        missing = self.machine("asset", "12005", "--meshes", "missing.zip",
                               code=4, reason="archive_missing")["diagnostics"][0]
        self.assertEqual(missing["path"], str(self.work / "missing.zip"))
        pointer_copy = self.work / "ugc_meshes_all.zip"
        pointer_copy.write_text(f"version https://git-lfs.github.com/spec/v1\noid sha256:{'0' * 64}\nsize 134\n")
        lfs = self.machine("asset", "12005", "--meshes", pointer_copy, "--obj", "a.obj",
                           code=4, reason="lfs_pointer")["diagnostics"][0]
        self.assertEqual(lfs["field"], "--meshes")
        self.assertEqual(lfs["path"], str(pointer_copy))
        self.assertFalse((self.work / "a.obj").exists())

    def test_invalid_project_reports_field_and_instance(self):
        project = self.work / "проект.json"
        self.machine("init", project, "--name", "CLI")
        self.machine("place", project, "first: test", "12005", "--at", "0", "0", "0", "--assembly", "Gate")
        data = json.loads(project.read_text())
        data["instances"].append(data["instances"][0])
        project.write_text(json.dumps(data))
        for args in (("validate", "--project", project), ("build", project, "invalid.pugc")):
            result = self.machine(*args, code=1, reason="invalid_project")
            problem = result["diagnostics"][0]
            self.assertEqual(problem["field"], "instances[1].id")
            self.assertEqual(problem["instance_id"], "first: test")
            self.assertEqual(problem["path"], str(project))
            self.assertEqual(result["artifacts"], {})
        project.write_text("{")
        self.machine("validate", "--project", project, code=1, reason="invalid_project")
        self.assertFalse((self.work / "invalid.pugc").exists())

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for PUGC")
    def test_house_scale_warning_allows_edit_and_build(self):
        project = self.work / "project.json"
        self.machine("init", project, "--name", "Buildings")
        self.machine("place", project, "dummy", "10243", "--at-cm", "0", "0", "0",
                     "--scale", "1.25", "1", "1", "--assembly", "Street")
        placed = self.machine("place", project, "home", "10049", "--at-cm", "0", "0", "0",
                              "--scale", "1.25", "1", "1", "--assembly", "Street")
        self.assertEqual([(row["code"], row["severity"], row["instance_id"]) for row in placed["diagnostics"]],
                         [("house_scale", "warning", "home")])
        revision = hashlib.sha256(project.read_bytes()).hexdigest()
        request = self.work / "request.json"
        request.write_text(json.dumps({"schema": 1, "base_sha256": revision, "operations": [
            {"op": "update", "id": "home", "fields": {"scale": [1, 1, 1.25]}}]}))
        edit = self.machine("edit", project, request, "--dry-run")
        self.assertEqual(edit["diagnostics"][0]["code"], "house_scale")
        binary = self.work / "map.pugc"
        built = self.machine("build", project, binary)
        self.assertEqual(built["diagnostics"][0]["code"], "house_scale")
        self.assertEqual(self.machine("validate", binary)["diagnostics"][0]["code"], "house_scale")

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for PUGC")
    def test_build_and_import_artifacts(self):
        project = self.work / "project.json"
        self.assertEqual(self.machine("init", project, "--name", "Маршрут")["artifacts"]["project"], str(project))
        placement = self.machine("place", project, "post", "12005", "--at", "1", "2", "3", "--assembly", "Gate")
        self.assertEqual(placement["data"]["instance_id"], "post")
        binary = self.work / "map.pugc"
        build = self.machine("build", project, binary, "--raw-json", "raw.json")
        digest = hashlib.sha256(binary.read_bytes()).hexdigest()
        self.assertEqual(build["data"]["pugc_sha256"], digest)
        self.assertEqual(set(build["artifacts"]), {"pugc", "receipt", "raw_json"})
        self.assertTrue(all(Path(value).is_file() for value in build["artifacts"].values()))
        self.assertEqual(self.machine("validate", binary)["data"]["pugc_sha256"], digest)
        loaded = self.machine("import", binary, "imported.json")
        self.assertTrue(loaded["data"]["used_receipt"])
        self.assertEqual(json.loads(Path(loaded["artifacts"]["project"]).read_text())["instances"][0]["id"], "post")
        legacy = self.cli("build", project, "legacy.pugc", machine=False)
        self.assertEqual(legacy.returncode, 0)
        self.assertEqual(legacy.stdout.strip(), hashlib.sha256((self.work / "legacy.pugc").read_bytes()).hexdigest())

    @unittest.skipUnless(shutil.which("blender") and shutil.which("openssl"), "Blender and OpenSSL required")
    def test_blender_resource_and_usage_statuses(self):
        self.machine("blender", "--blender", "/missing/pugc-blender", "audit", "--",
                     code=4, reason="blender_missing")
        self.machine("blender", "extract", "--", code=64, reason="invalid_arguments")
        subprocess.run([sys.executable, "-B", ROOT / "examples/ugc_route/prepare.py", self.work],
                       env=self.env, check=True, capture_output=True)
        archive = self.work / "empty.zip"
        with ZipFile(archive, "w"):
            pass
        partial = self.machine("blender", "--threads", "2", "audit", "--", "overpass.pugc", "--strict",
                               "--project", "overpass.json", "--out", "audit.json", "--meshes", archive,
                               code=4, reason="mesh_missing")
        self.assertEqual(partial["diagnostics"][0]["path"], str(archive))
        self.assertIsInstance(partial["diagnostics"][0]["object_id"], int)
        self.assertTrue(partial["diagnostics"][0]["instance_id"])
        report = json.loads(Path(partial["artifacts"]["report"]).read_text())
        self.assertEqual(report["pugc_sha256"], hashlib.sha256((self.work / "overpass.pugc").read_bytes()).hexdigest())
        self.assertEqual(report["geometry_check"]["status"], "partial")
        instances = report["geometry_check"]["checks"]["instances"]
        self.assertEqual((instances["expected"], instances["checked"]), (6, 0))
        self.assertIn("support-a", instances["missing"])

    @unittest.skipUnless(shutil.which("blender") and shutil.which("openssl"), "Blender and OpenSSL required")
    def test_blender_finding_has_exit_two_and_preserves_artifacts(self):
        source = os.getenv("UGC_TEST_SOURCE_NPZ")
        meshes = os.getenv("UGC_TEST_MESHES")
        if not source or not meshes:
            self.skipTest("Set UGC_TEST_SOURCE_NPZ and UGC_TEST_MESHES for comparison")
        subprocess.run([sys.executable, "-B", ROOT / "examples/ugc_route/prepare.py", self.work],
                       env=self.env, check=True, capture_output=True)
        args = ("blender", "--threads", "2", "compare", "--", Path(source).resolve(), "wrong-tower.pugc",
                "--project", "route.json", "--meshes", Path(meshes).resolve(),
                "--cameras", "Approach", "Alongside", "--samples", "1")
        mismatch = self.machine(*args, "--out", "frames", code=2, reason="geometry_mismatch")
        self.assertIn("Tower", mismatch["diagnostics"][0]["message"])
        self.assertEqual(len(mismatch["artifacts"]["result_frames"]), 2)
        self.assertEqual(mismatch["data"]["geometry_check"]["checks"]["landmarks"]["findings"], 2)
        human = self.cli(*args, "--out", "legacy-frames", machine=False)
        self.assertEqual(human.returncode, 2, (human.stdout, human.stderr))

        correct = list(args)
        correct[correct.index("wrong-tower.pugc")] = "route.pugc"
        accepted = self.machine(*correct, "--out", "correct-frames", "--strict")
        details = accepted["data"]["geometry_check"]
        self.assertEqual(details["status"], "complete")
        self.assertEqual(details["checks"]["landmarks"]["checked"], 2)
        self.assertEqual(details["checks"]["instances"]["checked"], 12)
        self.assertEqual(accepted["data"]["pugc_sha256"], hashlib.sha256((self.work / "route.pugc").read_bytes()).hexdigest())
        start = correct.index("--cameras")
        correct[start + 1:start + 3] = ["Missing camera"]
        partial = self.machine(*correct, "--out", "missing-camera", "--strict", code=4, reason="geometry_incomplete")
        self.assertEqual(partial["data"]["geometry_check"]["checks"]["cameras"]["missing"], ["Missing camera"])
        self.assertEqual(partial["artifacts"]["source_frames"], [])
        self.assertTrue(Path(partial["artifacts"]["report"]).is_file())


if __name__ == "__main__":
    unittest.main()
