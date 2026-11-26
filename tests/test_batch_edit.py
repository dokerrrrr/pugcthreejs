import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pugc_toolkit import batch_edit
from pugc_toolkit.pugc_codec import encode_raw, read_game
from pugc_toolkit.ugc_project import game_records, new_project, place, transform
from pugc_toolkit.ugc_storage import lock_outputs

ROOT = Path(__file__).resolve().parents[1]


class BatchEditTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="Пакет правок ")
        self.addCleanup(directory.cleanup)
        self.work = Path(directory.name)
        self.project = self.work / "project.json"
        self.request = self.work / "request.json"

    def cli(self, *args, code=0):
        command = [sys.executable, "-B", "-m", "pugc_toolkit", "--json", *args]
        result = subprocess.run(command, cwd=self.work, env={**os.environ, "PYTHONPATH": str(ROOT)},
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, code, (result.stdout, result.stderr))
        return json.loads(result.stdout)

    def plan(self, *operations):
        source = self.project.read_bytes()
        self.request.write_text(json.dumps({"schema": 1, "base_sha256": hashlib.sha256(source).hexdigest(),
                                            "operations": operations}), encoding="utf-8")
        return source

    @unittest.skipUnless(shutil.which("openssl"), "OpenSSL required for the import round trip")
    def test_one_of_two_imported_benches_and_unknown_fields(self):
        scene = new_project("Bench test")
        place(scene, "left", 16052, transform((1, 2, 3)), "Gate", "near")
        place(scene, "right", 16052, transform((5, 2, 3)), "Gate", "near")
        payload = game_records(scene)
        payload["customSetting"] = {"keep": True}
        left, right = payload["objects"]
        left["custom"] = {"name": "left"}
        left["spawnTransform"]["translation"]["unitTag"] = "cm"
        left["spawnTransform"]["rotation"]["rotationTag"] = "authored"
        right["custom"] = {"name": "right"}
        original_map, output_map = self.work / "before.pugc", self.work / "after.pugc"
        original_map.write_bytes(encode_raw(payload))
        self.cli("import", original_map, self.project)
        source = self.plan({"op": "update", "id": "import-0000", "fields": {"at_cm": [99800, 200, 300], "assembly": "Steps"}})
        files = set(self.work.iterdir())
        preview = self.cli("edit", self.project, self.request, "--dry-run")
        self.assertEqual(set(self.work.iterdir()), files)
        self.assertEqual(self.project.read_bytes(), source)
        self.assertEqual(preview["data"]["changes"], [{"id": "import-0000", "change": "update",
                                                       "fields": ["assembly", "spawnTransform.translation"]}])
        with patch.object(batch_edit, "atomic_write", wraps=batch_edit.atomic_write) as write:
            result = batch_edit.edit(self.project, self.request)
        self.assertEqual(write.call_count, 1)
        self.assertEqual(result["data"]["result_sha256"], preview["data"]["result_sha256"])
        self.assertEqual(self.cli("edit", self.project, self.request, code=5)["diagnostics"][0]["code"],
                         "revision_conflict")
        self.assertEqual(hashlib.sha256(self.project.read_bytes()).hexdigest(), result["data"]["result_sha256"])
        self.cli("build", self.project, output_map)
        reopened = read_game(output_map)
        self.assertEqual(reopened["customSetting"], payload["customSetting"])
        self.assertEqual(reopened["objects"][1], right)
        self.assertEqual(reopened["objects"][0]["custom"], left["custom"])
        self.assertNotEqual(reopened["objects"][0]["spawnTransform"], left["spawnTransform"])
        self.assertEqual(reopened["objects"][0]["spawnTransform"]["translation"]["unitTag"], "cm")
        self.assertEqual(reopened["objects"][0]["spawnTransform"]["rotation"]["rotationTag"], "authored")

    def test_last_error_approved_road_and_concurrent_writer_leave_project_unchanged(self):
        self.cli("init", self.project, "--name", "Road")
        self.cli("place", self.project, "road", "12005", "--at", "0", "0", "0", "--assembly", "Road",
                 "--zone", "road")
        self.cli("approve-road", self.project)
        add = {"op": "add", "id": "sign", "fields": {"objectId": 12005, "at_cm": [1, 2, 3], "assembly": "Gate"}}
        for tail, code in (
            ({"op": "delete", "id": "missing"}, "invalid_edit"),
            ({"op": "update", "id": "road", "fields": {"at_cm": [1, 2, 3]}}, "approved_road_changed"),
            ({"op": "delete", "id": "road"}, "approved_road_changed"),
        ):
            with self.subTest(tail=tail):
                source = self.plan(add, tail)
                output = self.cli("edit", self.project, self.request, code=1)
                self.assertEqual(output["diagnostics"][0]["code"], code)
                self.assertEqual(self.project.read_bytes(), source)
        source = self.plan(add)
        with lock_outputs((self.project,)):
            self.assertEqual(self.cli("edit", self.project, self.request, code=5)["diagnostics"][0]["code"],
                             "file_operation_failed")
        self.assertEqual(self.project.read_bytes(), source)

    def test_add_delete_noop_and_retry(self):
        self.cli("init", self.project, "--name", "Changes")
        self.cli("place", self.project, "delete-me", "12005", "--at", "0", "0", "0", "--assembly", "Gate")
        self.plan({"op": "delete", "id": "delete-me"},
                  {"op": "add", "id": "new", "fields": {"objectId": 12005, "at_cm": [0, 1, 2], "assembly": "Gate"}})
        self.cli("edit", self.project, self.request)
        self.cli("edit", self.project, self.request, code=5)
        self.assertEqual([row["id"] for row in json.loads(self.project.read_bytes())["instances"]], ["new"])
        before = self.plan({"op": "update", "id": "new", "fields": {"assembly": "Gate"}})
        for _ in range(2):
            result = self.cli("edit", self.project, self.request)
            self.assertEqual(result["data"]["edit_status"], "unchanged")
            self.assertEqual(result["data"]["changes"], [])
        self.assertEqual(self.project.read_bytes(), before)
