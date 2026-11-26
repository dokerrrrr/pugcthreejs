from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from pugc_codec import decode_raw
from ugc_project import PROFILE, approve_road, load_json, place, project_from_map, save_json, transform


def game(objects):
    return {"name": "Fixture", "dataVersion": 3, "buildVersion": "2608.1.1", "objects": objects,
            "deviceEventList": [], "customSetting": {"keep": True}}


class ProjectValidationCliTest(unittest.TestCase):
    def setUp(self):
        workspace = TemporaryDirectory()
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name)
        self.project = self.workspace / "project.json"
        self.binary = self.workspace / "map.pugc"
        self.sidecar = self.workspace / "map.pugc.json"
        self.raw_output = self.workspace / "game.json"
        self.scene = project_from_map(game([]))
        place(self.scene, "lamp", 12005, transform((1, 2, 3)), "Gate", "near")

    def run_cli(self, script, *args):
        return subprocess.run([sys.executable, "-B", ROOT / "tools" / script, *args], cwd=self.workspace,
                              capture_output=True, text=True)

    def preflight(self, *options):
        return self.run_cli("validate_pugc.py", "--project", self.project, *options)

    def build(self, *options):
        return self.run_cli("ugc_project.py", "build", self.project, self.binary,
                            "--raw-json", self.raw_output, *options)

    def assert_rejected(self, project, message, *options):
        save_json(self.project, project)
        preflight = self.preflight(*options)
        build = self.build(*options)
        self.assertEqual((preflight.returncode, build.returncode), (1, 1), (preflight.stderr, build.stderr))
        preliminary = json.loads(preflight.stdout)
        compilation = json.loads(build.stderr)
        self.assertEqual(preliminary["errors"], compilation["errors"])
        self.assertIn(message, " ".join(preliminary["errors"]))
        self.assertFalse(any(path.exists() for path in (self.binary, self.sidecar, self.raw_output)))
        return compilation

    def test_structure_checks_agree_before_any_output_is_written(self):
        duplicate = deepcopy(self.scene)
        duplicate["instances"].append(deepcopy(duplicate["instances"][0]))
        zero = deepcopy(self.scene)
        zero["instances"][0]["transform"]["rotation"]["w"] = 0
        missing_road = deepcopy(self.scene)
        missing_road["instances"][0]["zone"] = "road"
        approve_road(missing_road)
        missing_road["instances"].clear()

        for project, detail in (
            ({**self.scene, "schema": 9}, "schema"),
            (duplicate, "instances[1] ('lamp').id"),
            (missing_road, "approved_road['lamp']"),
            (zero, "instances[0] ('lamp').transform.rotation"),
            ({**self.scene, "landmarks": [{"result": ["missing"]}]}, "landmarks[0].result[0]"),
        ):
            with self.subTest(detail=detail):
                self.assert_rejected(project, detail)

    def test_unknown_id_and_selected_budget_are_checked_on_build(self):
        unknown = deepcopy(self.scene)
        unknown["instances"][0]["objectId"] = 987654
        report = self.assert_rejected(unknown, "instances[0] ('lamp').objectId")
        self.assertFalse(report["budget"][0]["complete"])

        custom = load_json(PROFILE)
        next(rule for rule in custom["rules"] if rule["key"] == "prop")["limit"] = 1
        profile_path = self.workspace / "limited.json"
        save_json(profile_path, custom)
        place(self.scene, "post", 12005, transform((2, 2, 3)), "Gate", "near")
        self.assert_rejected(self.scene, "Props: 2/1", "--profile", profile_path)
        self.assert_rejected(self.scene, "outside allowed IDs", "--allowed-ids", "13003")

    def test_clean_preflight_build_and_reopened_map_agree(self):
        save_json(self.project, self.scene)
        preflight = self.preflight()
        result = self.build()
        reopened = self.run_cli("validate_pugc.py", self.binary)
        self.assertEqual((preflight.returncode, result.returncode, reopened.returncode), (0, 0, 0))
        self.assertEqual(len(result.stdout.strip()), 64)
        self.assertEqual(json.loads(preflight.stdout)["budget"], json.loads(reopened.stdout)["budget"])

    def test_inherited_budget_may_be_preserved_but_not_increased(self):
        custom = load_json(PROFILE)
        next(rule for rule in custom["rules"] if rule["key"] == "prop")["limit"] = 1
        profile_path = self.workspace / "limited.json"
        save_json(profile_path, custom)
        objects = [
            {"objectId": 12005, "deviceIndex": -1, "userDeviceName": "",
             "spawnTransform": transform((x, 5, 3))}
            for x in (450, 460)
        ]
        project = project_from_map(game(objects))
        save_json(self.project, project)
        result = self.preflight("--profile", profile_path)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Props: 2/1 placement weight (inherited)", json.loads(result.stdout)["warnings"])
        place(project, "new-post", 12005, transform((3, 2, 1)), "Gate", "near")
        self.assert_rejected(project, "Props: 3/1", "--profile", profile_path)

    def test_legacy_scale_warnings_preserve_game_payload_and_block_new_defects(self):
        original = game([
            {"objectId": 12005, "deviceIndex": -1, "userDeviceName": "",
             "spawnTransform": transform((450.123456789123, 5, 3), 22, (.0999, 1, 1)),
             "unknownObjectField": {"values": ["держать", 12]}},
            {"objectId": 12005, "deviceIndex": -1, "userDeviceName": "",
             "spawnTransform": transform((460.123456789123, 5, 3), 28, (1, .1234, 1))},
            {"objectId": 2, "deviceIndex": 4, "userDeviceName": "Imported device",
             "spawnTransform": transform((440, 5, 3))},
        ])
        device = {"objectId": 2, "deviceIndex": 4}
        original["deviceEventList"] = [{
            "eventId": {"deviceInstanceId": device, "eventName": "Fixture"},
            "relationEventId": [{"deviceInstanceId": device, "eventName": "Action"}],
        }]
        project = project_from_map(original)
        save_json(self.project, project)
        preflight = self.preflight()
        build = self.build()
        self.assertEqual((preflight.returncode, build.returncode), (0, 0), (preflight.stdout, build.stderr))
        report = json.loads(build.stderr)
        self.assertEqual(report["warnings"], json.loads(preflight.stdout)["warnings"])
        self.assertEqual(len(report["warnings"]), 2)
        self.assertTrue(all("scale3D" in item and "inherited" in item for item in report["warnings"]))
        self.assertEqual(decode_raw(self.binary.read_bytes()), original)

        original_files = [path.read_bytes() for path in (self.binary, self.sidecar, self.raw_output)]
        project["instances"][0]["transform"]["scale3D"]["x"] = .08
        save_json(self.project, project)
        invalid = self.build()
        self.assertEqual(invalid.returncode, 1)
        self.assertIn("scale3D", " ".join(json.loads(invalid.stderr)["errors"]))
        self.assertEqual([path.read_bytes() for path in (self.binary, self.sidecar, self.raw_output)], original_files)


if __name__ == "__main__":
    unittest.main()
