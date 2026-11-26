from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from pugc_codec import decode_raw, encode_raw
from ugc_project import (approve_road, build, game_records, load_json, place, project_from_map,
                         read_with_receipt, save_json, transform)


class ProjectReceiptTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.source = self.directory / "Карта перед правкой.pugc"
        self.sidecar = self.source.with_suffix(".pugc.json")
        self.destination = self.directory / "Новый проект.json"
        raw = {
            "name": "Receipt fixture", "dataVersion": 3, "buildVersion": "2608.1.1",
            "deviceEventList": [], "unknownGameField": {"items": [3, "сохранить"]},
            "objects": [{
                "objectId": 10048, "deviceIndex": -1, "userDeviceName": "",
                "spawnTransform": transform((411.123456789123, 72.25, 3.7), 23),
                "unknownObjectField": {"value": True},
            }],
        }
        self.project = project_from_map(raw, 48321.25)
        self.project["instances"][0].update(
            id="first", assembly="Entrance", zone="near", role="fixture", gameplay=False,
            source="original", review={"ok": True},
        )
        place(self.project, "pavement", 10048, transform((420.987654321234, 2, 1), 12),
              "Pavement", "road")
        place(self.project, "post", 12005, transform((425, 3, 1)), "Gate", "shoulder")
        self.project["landmarks"] = [{"result": ["post"]}]
        approve_road(self.project)
        self.receipt = build(self.project, self.source)

    def run_cli(self, script, *args):
        return subprocess.run([sys.executable, "-B", ROOT / "tools" / script, *args],
                              cwd=self.directory, capture_output=True, text=True)

    def import_project(self, *options, source=None):
        return self.run_cli("ugc_project.py", "import", self.source if source is None else source,
                            self.destination, "--legacy-layout", *options)

    def test_verified_pair_round_trip(self):
        # The exact approval cannot be reconstructed from the rounded binary translation alone.
        authored, _ = read_with_receipt(self.source)
        self.assertNotEqual(authored["objects"][1]["spawnTransform"],
                            self.project["approved_road"]["pavement"]["transform"])

        result = self.import_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = load_json(self.destination)
        self.assertEqual(imported["origin_x_cm"], self.project["origin_x_cm"])
        self.assertEqual(imported["approved_road"], self.project["approved_road"])
        self.assertNotIn("landmarks", imported)

        output = self.directory / "rebuilt.pugc"
        result = self.run_cli("ugc_project.py", "build", self.destination, output)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_json(output.with_suffix(".pugc.json")), self.receipt)
        self.assertEqual(decode_raw(output.read_bytes()), decode_raw(self.source.read_bytes()))
        self.assertEqual([(entry["id"], entry["objectId"]) for entry in imported["instances"][:2]],
                         [("first", 10048), ("pavement", 10048)])

    def test_external_binary_and_deliberate_receipt_bypass(self):
        external = self.directory / "external.pugc"
        external.write_bytes(self.source.read_bytes())
        result = self.import_project(source=external)
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = load_json(self.destination)
        self.assertEqual(imported["origin_x_cm"], 50000)
        self.assertEqual([entry["id"] for entry in imported["instances"]],
                         ["import-0000", "import-0001", "import-0002"])
        self.assertEqual(imported["game"]["unknownGameField"], self.project["game"]["unknownGameField"])
        self.assertEqual(imported["instances"][0]["game_original"]["unknownObjectField"], {"value": True})

        self.sidecar.write_text("{", encoding="utf-8")
        result = self.import_project("--ignore-receipt", "--origin-x-cm", "48321.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = load_json(self.destination)
        self.assertEqual(imported["origin_x_cm"], 48321.25)
        self.assertEqual(imported["instances"][0]["id"], "import-0000")
        self.assertNotIn("approved_road", imported)

    def test_explicit_receipt_and_legacy_optional_approval(self):
        alternate = self.directory / "metadata.json"
        legacy_receipt = {k: v for k, v in self.receipt.items() if k != "approved_road"}
        save_json(alternate, legacy_receipt)
        self.sidecar.write_text("{", encoding="utf-8")
        result = self.import_project("--receipt", alternate, "--origin-x-cm", "48321.25")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(load_json(self.destination)["instances"][0]["id"], "first")

    def test_legacy_import_and_receipt_keep_game_placement(self):
        snapshot = transform((491.23, -27.89, 11.7), 0, (.8, 1.25, .35))
        stored = {
            "objectId": 15006, "deviceIndex": -1, "userDeviceName": "",
            "spawnTransform": {**snapshot, "translation": dict(x=50877, y=-2894.98617792, z=1170),
                               "rotation": dict(x=0, y=0, z=1, w=0)},
        }
        raw = {**self.project["game"], "objects": [stored]}
        encoded = encode_raw(raw)
        self.source.write_bytes(encoded)
        metadata = {"id": "road", "objectId": 15006, "assembly": "Road", "zone": "road"}
        approvals = {"road": {"objectId": 15006, "transform": snapshot}}
        save_json(self.sidecar, {"schema": 1, "pugc_sha256": sha256(encoded).hexdigest(),
                                 "origin_x_cm": 50000, "instances": [metadata], "approved_road": approvals})
        result = self.import_project()
        self.assertEqual(result.returncode, 0, result.stderr)
        imported = load_json(self.destination)
        current = imported["instances"][0]["transform"]
        self.assertEqual(current["translation"], dict(x=49123, y=-2894.98617792, z=1170))
        self.assertEqual(current["rotation"], dict(x=0, y=0, z=-1, w=0))
        self.assertEqual(imported["approved_road"]["road"]["transform"], current)
        self.assertEqual(game_records(imported), raw)

        legacy = {**imported, "instances": [{**metadata, "transform": snapshot, "game_original": stored}],
                  "approved_road": approvals}
        legacy.pop("layout_version")
        self.assertEqual(game_records(legacy), raw)
        output = self.directory / "legacy-rebuilt.pugc"
        self.assertEqual(build(legacy, output)["approved_road"], imported["approved_road"])
        self.assertEqual(decode_raw(output.read_bytes()), raw)

        legacy.pop("approved_road")
        snapshot["translation"]["x"] += 100
        updated = game_records(legacy)["objects"][0]["spawnTransform"]
        self.assertEqual(updated["translation"], dict(x=50777, y=-2894.98617792, z=1170))
        self.assertEqual(updated["rotation"], stored["spawnTransform"]["rotation"])

    def test_legacy_partial_edits_preserve_the_other_game_fields(self):
        source = {
            "objectId": 15006, "deviceIndex": -1, "userDeviceName": "",
            "spawnTransform": transform((500, 100, 6)),
            "unknownObjectField": {"value": True},
        }
        expected = source["spawnTransform"]
        legacy = {
            "schema": 1, "origin_x_cm": 50000, "game": self.project["game"],
            "instances": [{
                "id": "object", "objectId": source["objectId"], "assembly": "Fixture", "zone": "near",
                "game_original": source,
                "transform": {
                    **expected, "translation": dict(x=50000, y=9915.21105766, z=600),
                    "rotation": dict(x=0, y=0, z=1, w=0),
                },
            }],
        }
        cases = (
            ({"at_m": [501, 99.1521105766, 6]}, {"translation": dict(x=49900, y=10000, z=600)}),
            ({"scale": [1, 2, 1]}, {"scale3D": dict(x=1, y=2, z=1)}),
            ({"rotation_xyzw": [0, 0, 0, 1]}, {"rotation": dict(x=0, y=0, z=1, w=0)}),
        )
        request = self.directory / "edit.json"
        for index, (fields, changed) in enumerate(cases):
            with self.subTest(fields=fields):
                save_json(self.destination, legacy)
                save_json(request, {
                    "schema": 1, "base_sha256": sha256(self.destination.read_bytes()).hexdigest(),
                    "operations": [{"op": "update", "id": "object", "fields": fields}],
                })
                result = self.run_cli("ugc_project.py", "edit", self.destination, request)
                self.assertEqual(result.returncode, 0, result.stderr)
                output = self.directory / f"edited-{index}.pugc"
                result = self.run_cli("ugc_project.py", "build", self.destination, output)
                self.assertEqual(result.returncode, 0, result.stderr)
                actual = decode_raw(output.read_bytes())["objects"][0]
                self.assertEqual(actual, {**source, "spawnTransform": {**expected, **changed}})

        save_json(self.destination, legacy)
        result = self.run_cli("ugc_project.py", "rotate", self.destination, "Fixture",
                              "--center", "500", "99.1521105766", "6", "--yaw", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        rotated = game_records(load_json(self.destination))["objects"][0]["spawnTransform"]
        self.assertEqual(rotated, {**expected, "rotation": transform((0, 0, 0), -10)["rotation"]})

    def test_invalid_receipts_fail_before_writing_the_project(self):
        entries = self.receipt["instances"]
        changed_road = deepcopy(self.receipt["approved_road"])
        changed_road["pavement"]["transform"]["translation"]["x"] += 10
        invalid_rotation = deepcopy(self.receipt["approved_road"])
        invalid_rotation["pavement"]["transform"]["rotation"] = dict.fromkeys("xyzw", 0)
        changes = (
            ({"pugc_sha256": "0" * 64}, "SHA-256"),
            ({"schema": 3}, "unsupported receipt schema"),
            ({"origin_x_cm": True}, "finite number"),
            ({"instances": [{**entries[0], "id": entries[1]["id"]}, *entries[1:]]}, "unique string"),
            ({"instances": entries[:-1]}, "object count"),
            ({"instances": [entries[2], *entries[:2]]}, "same index"),
            ({"approved_road": changed_road}, "approved_road"),
            ({"approved_road": invalid_rotation}, "approved_road"),
        )
        for change, message in changes:
            with self.subTest(field=next(iter(change)), error=message):
                save_json(self.sidecar, {**self.receipt, **change})
                result = self.import_project()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
                self.assertFalse(self.destination.exists())

        report = self.run_cli("validate_pugc.py", self.source)
        self.assertNotEqual(report.returncode, 0)
        self.assertIn("approved_road", report.stderr)
        with self.assertRaisesRegex(ValueError, "approved_road"):
            read_with_receipt(self.source)

        self.destination.write_bytes(b"existing project")
        save_json(self.sidecar, self.receipt)
        result = self.import_project("--origin-x-cm", "50000")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("origin override", result.stderr)
        self.assertEqual(self.destination.read_bytes(), b"existing project")

    def test_failed_publication_removes_the_staged_project(self):
        self.destination.mkdir()
        before = set(self.directory.iterdir())
        result = self.import_project()
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(self.destination.is_dir())
        self.assertEqual(set(self.directory.iterdir()), before)


if __name__ == "__main__":
    unittest.main()
