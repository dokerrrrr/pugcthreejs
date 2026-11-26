from hashlib import sha256
import json
import math
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from pugc_toolkit.pugc_codec import read_game
from pugc_toolkit.project_migration import migrate
from pugc_toolkit.ugc_project import game_records, load_json

FIXTURES = Path(__file__).parent / "fixtures" / "blender"


class GameSpaceTest(unittest.TestCase):
    def run_command(self, *arguments, code=0):
        result = subprocess.run([sys.executable, "-B", "-m", "pugc_toolkit", "--json", *arguments],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def test_cli_roundtrip_without_legacy_conversion(self):
        with TemporaryDirectory() as tmp:
            p = Path(tmp)
            project, binary = p / "project.json", p / "map.pugc"
            self.run_command("init", project, "--name", "Asymmetric post")
            self.run_command("place", project, "post", "12005", "--at-cm", "51003.125", "51200", "1064.0625",
                             "--yaw-deg", "90", "--scale", "2", "3", "4", "--assembly", "Gate")
            from pugc_toolkit.ugc_project import build
            with patch("pugc_toolkit.ugc_project.convert_layout", side_effect=AssertionError):
                build(load_json(project), binary)
            binary_cli = p / "cli.pugc"
            self.run_command("build", project, binary_cli)
            self.assertEqual(binary.read_bytes(), binary_cli.read_bytes())
            raw, = read_game(binary)["objects"]
            t = raw["spawnTransform"]
            self.assertEqual(t["translation"], {"x": 51003.125, "y": 51200, "z": 1064.0625})
            self.assertEqual(t["scale3D"], {"x": 2, "y": 3, "z": 4})
            self.assertAlmostEqual(t["rotation"]["z"], math.sqrt(.5))
            self.assertAlmostEqual(t["rotation"]["w"], math.sqrt(.5))
            self.assertEqual(load_json(binary.with_suffix(".pugc.json"))["schema"], 2)
            self.assertTrue(self.run_command("validate", binary)["data"]["valid"])
            lookup = self.run_command("inspect", project, "--id", "post")
            self.assertEqual(lookup["data"]["placements"][0]["spawnTransform"], t)

    def test_legacy_pair_import_and_old_project_migration(self):
        original = FIXTURES / "route.pugc"
        raw = read_game(original)
        project = migrate(load_json(FIXTURES / "route.json"))
        self.assertEqual(game_records(project), raw)
        self.assertEqual(project["annotation_frame"]["coordinate_space"], "legacy-authoring-xyz-m")
        with TemporaryDirectory() as tmp:
            p = Path(tmp)
            draft = p / "imported.json"
            self.run_command("import", original, draft)
            self.assertEqual(game_records(load_json(draft)), raw)
            self.assertEqual(load_json(draft)["instances"][5]["id"], "tower")
            self.assertEqual(load_json(draft)["instances"][5]["spawnTransform"]["translation"], {"x": 100800, "y": 1000, "z": 105})
            request = p / "edit.json"
            request.write_text(json.dumps({"schema": 1, "base_sha256": sha256(draft.read_bytes()).hexdigest(),
                "operations": [{"op": "update", "id": "tower", "fields": {"at_cm": [100801, 1000, 105]}}]}))
            self.run_command("edit", draft, request)
            self.run_command("edit", draft, request, code=5)
            output = p / "edited.pugc"
            self.run_command("build", draft, output)
            changed = [i for i, (old, new) in enumerate(zip(raw["objects"], read_game(output)["objects"], strict=True))
                       if old != new]
            self.assertEqual(changed, [5])
