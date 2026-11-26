import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / "tools"
sys.path.insert(0, str(TOOLS))

from pugc_toolkit.game_space import transform_cm
from ugc_project import build, load_json, native_project_from_map, place, raw_with_receipt, save_json
from ugc_storage import PublicationError


class PublicationTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.workspace = Path(directory.name)
        self.project = self.workspace / "project.json"
        self.binary = self.workspace / "map.pugc"
        self.sidecar = self.workspace / "map.pugc.json"
        raw_directory = self.workspace / "Raw data"
        raw_directory.mkdir()
        self.raw = raw_directory / "raw.json"
        self.scene = native_project_from_map({
            "name": "Fixture", "dataVersion": 3, "buildVersion": "2608.1.1", "objects": [], "deviceEventList": [],
        })
        place(self.scene, "first", 12005, transform_cm((100, 200, 300)), "Example", "near")
        save_json(self.project, self.scene)

    def cli(self, *args):
        return subprocess.run([sys.executable, "-B", TOOLS / "ugc_project.py", *args],
                              cwd=self.workspace, capture_output=True, text=True)

    def snapshot(self):
        return tuple(path.read_bytes() for path in (self.binary, self.sidecar, self.raw))

    def test_output_aliases_and_import_source_are_protected(self):
        original_project = self.project.read_bytes()
        for args in (
            ("build", self.project, self.project),
            ("build", self.project, self.binary, "--raw-json", self.project),
            ("build", self.project, self.binary, "--raw-json", self.sidecar),
            ("game-json", self.project, self.project),
        ):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Conflicting input or output path", result.stderr)
                self.assertEqual(self.project.read_bytes(), original_project)
                self.assertFalse(self.binary.exists())
                self.assertFalse(self.sidecar.exists())
        build(self.scene, self.binary, self.raw)
        original = self.snapshot()
        imported = self.workspace / "imported.json"
        result = self.cli("import", self.binary, imported)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.cli("build", imported, self.binary)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Conflicting input or output path", result.stderr)
        self.assertEqual(self.snapshot(), original)

    def test_ordinary_failure_restores_all_outputs(self):
        build(self.scene, self.binary, self.raw)
        original = self.snapshot()
        place(self.scene, "second", 12005, transform_cm((400, 500, 600)), "Example", "near")
        replace = Path.replace

        def fail_on_second_publish(source, target):
            if source.suffix == ".new" and target == self.sidecar:
                raise OSError("injected publication failure")
            return replace(source, target)

        with patch.object(Path, "replace", fail_on_second_publish):
            with self.assertRaisesRegex(OSError, "injected"):
                build(self.scene, self.binary, self.raw)
        self.assertEqual(self.snapshot(), original)
        self.assertFalse(list(self.workspace.glob(".ugc-transaction-*.json")))
        raw_with_receipt(self.binary)

    def test_interruption_between_replacements_leaves_recovery_available(self):
        build(self.scene, self.binary, self.raw)
        original = self.snapshot()
        place(self.scene, "second", 12005, transform_cm((400, 500, 600)), "Example", "near")
        save_json(self.project, self.scene)
        script = """
import os
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from ugc_project import build, load_json
replace = Path.replace
def interrupt(source, target):
    if source.suffix == ".new" and target.name.endswith(".pugc.json"):
        os._exit(73)
    return replace(source, target)
Path.replace = interrupt
build(load_json(sys.argv[2]), sys.argv[3], sys.argv[4])
"""
        interrupted = subprocess.run([sys.executable, "-B", "-c", script, TOOLS, self.project,
                                      self.binary, self.raw], cwd=self.workspace)
        self.assertEqual(interrupted.returncode, 73)
        self.assertNotEqual(self.binary.read_bytes(), original[0])
        self.assertEqual(self.sidecar.read_bytes(), original[1])
        with self.assertRaisesRegex(PublicationError, "run recover"):
            raw_with_receipt(self.binary)
        wrong = self.cli("recover", self.binary)
        self.assertNotEqual(wrong.returncode, 0)
        self.assertIn("different output paths", wrong.stderr)
        self.raw.write_bytes(b"another writer")
        changed = self.cli("recover", self.binary, "--raw-json", self.raw)
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("Output changed since the interrupted build", changed.stderr)
        self.assertEqual(self.raw.read_bytes(), b"another writer")
        self.raw.write_bytes(original[2])
        recovered = self.cli("recover", self.binary, "--raw-json", self.raw)
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        self.assertEqual(json.loads(recovered.stdout)["status"], "restored")
        self.assertEqual(self.snapshot(), original)
        self.assertEqual(json.loads(self.cli("recover", self.binary, "--raw-json", self.raw).stdout)["status"],
                         "none")
        raw_with_receipt(self.binary)

    def test_concurrent_project_edit_gets_conflict_and_can_retry(self):
        script = """
from pathlib import Path
import sys
sys.path.insert(0, sys.argv[1])
from ugc_storage import lock_outputs
with lock_outputs((Path(sys.argv[2]),)):
    print("locked", flush=True)
    sys.stdin.readline()
"""
        args = ("place", self.project, "second", "12005", "--at-cm", "400", "500", "600", "--assembly", "Example")
        with subprocess.Popen([sys.executable, "-B", "-c", script, TOOLS, self.project],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as writer:
            try:
                self.assertEqual(writer.stdout.readline().strip(), "locked")
                result = self.cli(*args)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Another command is writing", result.stderr)
                self.assertEqual(len(load_json(self.project)["instances"]), 1)
            finally:
                writer.communicate("\n", timeout=10)
        result = self.cli(*args)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([item["id"] for item in load_json(self.project)["instances"]], ["first", "second"])


if __name__ == "__main__":
    unittest.main()
