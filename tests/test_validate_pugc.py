from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from pugc_codec import encode_raw
from ugc_project import PROFILE, load_json, transform
from validate_pugc import validate


def obj(oid):
    return dict(objectId=oid, spawnTransform=transform((0, 0, 0)), deviceIndex=-1, userDeviceName="")


def map_data(objects):
    return dict(name="Fixture", dataVersion=3, buildVersion="2608.1.1", objects=objects, deviceEventList=[])


class ValidatorTest(unittest.TestCase):
    def setUp(self):
        self.profile = load_json(PROFILE)

    def test_house_rows_and_zones(self):
        objects = [obj(16197)] * 350 + [obj(10008)] * 115 + [obj(10247)] * 396
        result = validate(map_data(objects), self.profile, ["road"]*350 + ["near"]*115 + ["far"]*396)
        rows = {row["key"]: row for row in result["budget"]}
        self.assertEqual((rows["MediumHouse"]["weight"], rows["MediumHouse"]["limit"]), (115, 5))
        self.assertEqual((rows["SmallHouse"]["weight"], rows["SmallHouse"]["limit"]), (396, 40))
        self.assertEqual(rows["prop"]["available_after_road_and_shoulder"], 2650)
        self.assertEqual(rows["prop"]["zones"], dict(road=350, shoulder=0, near=115, far=396, unclassified=0))
        self.assertFalse(result["valid"])

    def test_saved_map_reports_positive_out_of_profile_scales_as_warnings(self):
        entry = obj(12005)
        entry["spawnTransform"]["scale3D"] = {"x": .08973, "y": 2.600000143, "z": 1}
        with TemporaryDirectory() as root:
            path = Path(root) / "saved.pugc"
            command = [sys.executable, "-B", ROOT / "tools/validate_pugc.py", path]
            path.write_bytes(encode_raw(map_data([entry])))
            report = subprocess.run(command, capture_output=True, text=True, cwd=root)
            self.assertEqual(report.returncode, 0, report.stderr)
            self.assertIn("outside the authoring profile", json.loads(report.stdout)["warnings"][0])

            strict = subprocess.run([*command, "--strict-scale"], capture_output=True, text=True, cwd=root)
            self.assertEqual(strict.returncode, 1)
            self.assertIn("scale3D", json.loads(strict.stdout)["errors"][0])

            entry["spawnTransform"]["scale3D"]["x"] = 0
            path.write_bytes(encode_raw(map_data([entry])))
            invalid = subprocess.run(command, capture_output=True, text=True, cwd=root)
            self.assertEqual(invalid.returncode, 1)
            self.assertIn("scale3D", json.loads(invalid.stdout)["errors"][0])

    def test_invalid_transform_id_record_and_weight(self):
        invalid = obj(99999)
        invalid["spawnTransform"]["translation"]["x"] = float("inf")
        invalid["spawnTransform"]["rotation"]["w"] = 0
        invalid["spawnTransform"]["scale3D"]["z"] = .083
        invalid.pop("userDeviceName")
        known = obj(16197)
        profile = deepcopy(self.profile)
        profile["objects"]["16197"]["weight"] = 3
        result = validate(map_data([known, invalid, None]), profile)
        self.assertFalse(result["valid"])
        errors = " ".join(result["errors"])
        for name in ("objectId", "translation", "rotation", "scale3D", "userDeviceName", "objects[2]"):
            self.assertIn(name, errors)
        props = next(row for row in result["budget"] if row["key"] == "prop")
        self.assertEqual(props["weight"], 3)
        self.assertEqual(props["count"], 1)
        self.assertFalse(props["complete"])


if __name__ == "__main__":
    unittest.main()
