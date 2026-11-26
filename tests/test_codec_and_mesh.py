from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import pugc_codec
from mesh_library import MeshLibrary
from pugc_codec import decode_raw, encode_raw
from ugc_geometry import apply_matrix, placement_matrix
from ugc_project import MESHES, build, load_json, new_project, place, project_from_map, transform


def game_with_device():
    prop = {
        "objectId": 12005, "deviceIndex": -1, "userDeviceName": "",
        "spawnTransform": transform((497.123456789123, 12.3, 4), 21, (.7, 1.17, 1.2)),
        "unknownObjectField": {"values": [7, "keep"]},
    }
    device = {
        "objectId": 2, "deviceIndex": 4, "userDeviceName": "Старт + α",
        "spawnTransform": transform((503.123456789123, 472.123456789123, 212.1), 31, (.7, 1.17, 1.2)),
        "userDeviceData": {"anyUnmappedField": ["document", {"size": 2}]},
    }
    return {
        "name": "Donor", "dataVersion": 3, "buildVersion": "2608.2.1",
        "creatorData": {
            "name": "Donor", "description": "Old description", "versionMajor": 2,
            "ownerAccountId": "fixture-account", "ownerNickName": "Fixture owner",
        },
        "editFileData": {"createAccountId": "fixture-account", "createTime": 1234},
        "roundRuleData": {"roundGameCount": 2, "timeLimit": 300},
        "characterRuleData": {"characterHP": 100},
        "gameRuleData": {},
        "userSelectableData": {"modUserSelectPlayType": "NONE"},
        "unknownGameSection": {"items": [True, 42]},
        "objects": [prop, device],
        "deviceEventList": [{
            "eventId": {"deviceInstanceId": 4, "eventName": "FixtureEvent"},
            "relationEventId": [{"deviceInstanceId": 4, "eventName": "FixtureAction"}],
        }],
    }


class CodecTest(unittest.TestCase):
    def test_uniform_orientation_with_anisotropic_scale(self):
        authored = transform((491.23, -27.89, 11.7), 0, (.8, 1.25, .35))
        authored["rotation"] = dict(x=.1, y=.2, z=.3, w=.9)
        # These IDs used all three historical axes. A device and an unlisted ID
        # must also use the same orientation and keep their native origins.
        scene = {"objects": [{"objectId": oid, "deviceIndex": device, "spawnTransform": authored}
                             for oid, device in ((10005, -1), (15006, -1), (10007, -1), (2, 4), (99999, -1))]}
        before = deepcopy(scene)
        encoded = pugc_codec.encode(scene, origin_x_cm=48000)
        transforms = [entry["spawnTransform"] for entry in decode_raw(encoded)["objects"]]
        expected = {"translation": dict(x=46877, y=-2789, z=1170),
                    "rotation": dict(x=.1, y=-.2, z=-.3, w=.9), "scale3D": authored["scale3D"]}
        self.assertEqual(transforms, [expected] * len(scene["objects"]))
        self.assertEqual(pugc_codec.decode(encoded, origin_x_cm=48000), before)
        self.assertEqual(scene, before)

        local = np.array([[2, .7, 3.5], [-1, -2.3, 5.1], [.2, .1, 0]])
        ground_truth = apply_matrix(local, placement_matrix(authored))
        ground_truth[:, 0] = 960 - ground_truth[:, 0]
        np.testing.assert_allclose(apply_matrix(local * [-1, 1, 1], placement_matrix(expected)),
                                   ground_truth, atol=1e-8)

    def test_rewriting_game_file_preserves_raw_transforms_and_metadata(self):
        device_scene = game_with_device()
        static_scene = deepcopy(device_scene)
        static_scene["objects"].pop()
        static_scene["deviceEventList"].clear()
        with tempfile.TemporaryDirectory() as root:
            output = Path(root) / "resaved.pugc"
            for data in (static_scene, device_scene):
                build(project_from_map(data), output)
                self.assertEqual(data, decode_raw(output.read_bytes()))

    def test_explicit_template_preserves_metadata_and_clears_scene(self):
        donor = game_with_device()
        donor["modObjects"] = donor["objects"]
        donor["deviceConnections"] = [{"from": 4, "to": 4}]
        donor["deviceEventLinks"] = [{"event": "FixtureEvent"}]
        with tempfile.TemporaryDirectory() as root:
            donor_path, project_path, output = (Path(root) / name for name in
                                                ("Чистый шаблон.pugc", "project.json", "map.pugc"))
            original = encode_raw(donor)
            donor_path.write_bytes(original)
            subprocess.run([sys.executable, "-B", ROOT / "tools/ugc_project.py", "init", project_path,
                            "--name", "Новая карта", "--template", donor_path], check=True, cwd=root)
            project = load_json(project_path)
            self.assertEqual(project["instances"], [])
            self.assertNotIn("objects", project["game"])
            build(project, output)
            self.assertEqual(donor_path.read_bytes(), original)
            result = decode_raw(output.read_bytes())

        expected = deepcopy(donor)
        expected["name"] = "Новая карта"
        expected["creatorData"].update(name="Новая карта", description="")
        expected.pop("modObjects")
        for field in ("objects", "deviceEventList", "deviceConnections", "deviceEventLinks"):
            expected[field] = []
        self.assertEqual(result, expected)

    def test_version_source_supplies_only_the_build_version(self):
        donor = game_with_device()
        donor["buildVersion"] = "2612.9.7"
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, fresh, existing, output = (root / name for name in
                                               ("recent.pugc", "fresh.json", "existing.json", "map.pugc"))
            source.write_bytes(encode_raw(donor))
            cli = [sys.executable, "-B", ROOT / "tools/ugc_project.py"]
            subprocess.run([*cli, "init", fresh, "--name", "Blank", "--version-from", source],
                           check=True, cwd=root)
            subprocess.run([*cli, "init", existing, "--name", "Blank"], check=True, cwd=root)
            baseline = load_json(existing)
            existing_bytes = existing.read_bytes()
            subprocess.run([*cli, "build", existing, output, "--version-from", source],
                           check=True, cwd=root)
            updated = decode_raw(output.read_bytes())
            self.assertEqual(existing.read_bytes(), existing_bytes)
            self.assertEqual(updated, {**baseline["game"], "buildVersion": donor["buildVersion"], "objects": []})
            self.assertEqual(decode_raw(source.read_bytes()), donor)

            source.unlink()
            subprocess.run([*cli, "build", fresh, output], check=True, cwd=root)
            result = decode_raw(output.read_bytes())
            for field in ("createTime", "lastModifiedTime"):
                self.assertGreater(result["editFileData"][field], 0)
                result["editFileData"][field] = updated["editFileData"][field]
            self.assertEqual(result, updated)

    def test_invalid_version_source_does_not_replace_outputs(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            source, project, output = (root / name for name in ("source.pugc", "project.json", "map.pugc"))
            cli = [sys.executable, "-B", ROOT / "tools/ugc_project.py"]
            subprocess.run([*cli, "init", project, "--name", "Blank"], check=True, cwd=root)
            subprocess.run([*cli, "build", project, output], check=True, cwd=root)
            sidecar = output.with_suffix(".pugc.json")
            previous = [path.read_bytes() for path in (project, output, sidecar)]
            for data, diagnostic in (
                (b".pugc", "Truncated PUGC header"),
                (encode_raw({"dataVersion": 4, "buildVersion": "2612.1.1"}), "dataVersion 3"),
                (encode_raw({"dataVersion": 3, "buildVersion": " "}), "nonempty buildVersion"),
            ):
                source.write_bytes(data)
                for command in ([*cli, "init", project, "--name", "Blank"], [*cli, "build", project, output]):
                    with self.subTest(diagnostic=diagnostic, command=command[3]):
                        result = subprocess.run([*command, "--version-from", source], capture_output=True,
                                                text=True, cwd=root)
                        self.assertNotEqual(result.returncode, 0)
                        self.assertIn(diagnostic, result.stderr)
                        self.assertEqual([path.read_bytes() for path in (project, output, sidecar)], previous)

    def test_cli_creates_maps_from_an_installation_without_demo_maps_or_assets(self):
        with tempfile.TemporaryDirectory() as root:
            work = Path(root) / "Рабочая папка"
            work.mkdir()
            toolkit = work.parent / "toolkit"
            for folder in ("tools", "catalog", "pugc_toolkit"):
                shutil.copytree(ROOT / folder, toolkit / folder, ignore=shutil.ignore_patterns("__pycache__"))
            cli = [sys.executable, "-B", toolkit / "tools/ugc_project.py"]
            project, output = work / "project.json", work / "map.pugc"
            subprocess.run([*cli, "init", project, "--name", "Новая карта"], check=True, cwd=work)
            for count in (0, 1):
                with self.subTest(objects=count):
                    if count:
                        subprocess.run([*cli, "place", project, "post", "12005", "--at", "1", "2", "3",
                                        "--assembly", "Fixture"], check=True, cwd=work)
                    subprocess.run([*cli, "build", project, output], check=True, cwd=work, capture_output=True)
                    report = subprocess.check_output(
                        [sys.executable, "-B", toolkit / "tools/validate_pugc.py", output], cwd=work
                    )
                    result = decode_raw(output.read_bytes())
                    self.assertTrue(json.loads(report)["valid"])
                    self.assertEqual(result["name"], "Новая карта")
                    self.assertEqual(result["dataVersion"], 3)
                    self.assertEqual(result["creatorData"]["ownerAccountId"], "")
                    self.assertEqual(result["editFileData"]["createAccountId"], "")
                    self.assertEqual(result["editFileData"]["editPlayers"], [])
                    self.assertEqual(result["deviceEventList"], [])
                    self.assertEqual([obj["objectId"] for obj in result["objects"]], [12005] * count)

    @unittest.skipUnless(os.getenv("UGC_TEST_JS_CODEC") and shutil.which("node"),
                         "Set UGC_TEST_JS_CODEC to the pinned upstream browser codec")
    def test_node_reads_and_rewrites_empty_and_single_object_maps(self):
        codec = os.environ["UGC_TEST_JS_CODEC"]
        script = ROOT / "tools/pugc_portable.mjs"
        with tempfile.TemporaryDirectory() as root:
            source, intermediate, result = (Path(root) / name for name in ("map.pugc", "game.json", "output.pugc"))
            project = new_project("Новая карта")
            for count in (0, 1):
                with self.subTest(objects=count):
                    if count:
                        place(project, "post", 12005, transform((1, 2, 3)), "Fixture", "near")
                    build(project, source)
                    expected = decode_raw(source.read_bytes())
                    subprocess.run(["node", script, "decode", codec, source, intermediate], check=True)
                    self.assertEqual(load_json(intermediate), expected)
                    subprocess.run(["node", script, "encode", codec, intermediate, result], check=True)
                    self.assertEqual(decode_raw(result.read_bytes()), expected)


class MeshTest(unittest.TestCase):
    def test_full_mesh_after_sample_and_material_sections(self):
        archive = Path(os.environ.get("UGC_TEST_MESHES", MESHES))
        if not archive.exists() or archive.stat().st_size < 200:
            self.skipTest("Provide UGC_TEST_MESHES or Git LFS mesh archive")
        with MeshLibrary(archive) as library:
            sampled, small = library.load(15006, cap_vertices=37)
            measured, points = library.load(15006)
            geometry = library.geometry(15006)
            self.assertEqual(len(small), 37)
            self.assertEqual(len(points), 44571)
            self.assertEqual(sampled["bounds_min"], measured["bounds_min"])
            self.assertEqual(sampled["bounds_max"], measured["bounds_max"])
            self.assertEqual([s.first_triangle for s in geometry.sections], [0, 5833, 12793])
            self.assertEqual(sum(s.triangle_count for s in geometry.sections), len(geometry.triangles))
            self.assertEqual(geometry.triangle_uvs.shape, (14857, 3, 2))
        with MeshLibrary(archive) as library:
            np.testing.assert_array_equal(points, library.load(15006)[1])
        with MeshLibrary(archive, authoring=True) as library:
            mirrored = library.geometry(15006)
            np.testing.assert_array_equal(mirrored.vertices, geometry.vertices * [-1, 1, 1])
            np.testing.assert_array_equal(mirrored.triangles, geometry.triangles[:, [0, 2, 1]])
            np.testing.assert_array_equal(mirrored.triangle_uvs, geometry.triangle_uvs[:, [0, 2, 1]])


if __name__ == "__main__":
    unittest.main()
