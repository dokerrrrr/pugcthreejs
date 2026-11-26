import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / name)
                for name in ("tools", "examples/ugc_route")]
from mesh_library import MeshLibrary
from prepare import route_project
from ugc_project import (MESHES, PROFILE, ROOT, TEMPLATE, asset_card, build, game_records, instance, load_json,
                         read_with_receipt, rotate_assembly)


class ProjectTest(unittest.TestCase):
    def test_cli_from_external_workspace_leaves_installation_untouched(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            toolkit, workspace, cwd = (base / name for name in ("Toolkit", "Моя карта", "cwd"))
            for name in ("tools", "catalog", "pugc_toolkit"):
                shutil.copytree(ROOT / name, toolkit / name, ignore=shutil.ignore_patterns("__pycache__"))
            template_copy = toolkit / TEMPLATE.relative_to(ROOT)
            if not template_copy.is_file():
                template_copy.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(TEMPLATE, template_copy)
            workspace.mkdir()
            cwd.mkdir()
            before = set(toolkit.rglob("*"))
            env = {key: value for key, value in os.environ.items()
                   if key not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}

            def run(script, *args):
                result = subprocess.run([sys.executable, toolkit / "tools" / script, *args],
                                        cwd=cwd, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

            project = workspace / "project.json"
            built = workspace / "map.pugc"
            imported = workspace / "imported.json"
            run("ugc_project.py", "init", project, "--name", "Моя карта")
            run("ugc_project.py", "place", project, "west-post", "12005",
                "--at", "-3.9", "7", "1.48", "--assembly", "Gate")
            run("ugc_project.py", "build", project, built)
            run("validate_pugc.py", built)
            run("ugc_project.py", "import", built, imported)

            self.assertEqual(load_json(imported)["instances"][0]["objectId"], 12005)
            results = {path.name for path in workspace.iterdir() if not path.name.startswith(".ugc-lock-")}
            self.assertEqual(results, {"project.json", "map.pugc", "map.pugc.json", "imported.json"})
            self.assertEqual(set(toolkit.rglob("*")), before)
            self.assertEqual(list(cwd.iterdir()), [])

    def test_only_selected_bench_changes_in_written_pugc(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = route_project()
            before, after = [Path(tmp) / name for name in ("before.pugc", "after.pugc")]
            build(project, before)
            rotate_assembly(project, "East bench", (5.8, 5, 0), 180)
            build(project, after)
            first, _ = read_with_receipt(before)
            second, meta = read_with_receipt(after)
        self.assertEqual(sum(item["objectId"] == 16052 for item in second["objects"]), 2)
        changed = [entry["id"] for a, b, entry in zip(first["objects"], second["objects"], meta["instances"])
                   if a != b]
        self.assertEqual(changed, ["bench-east"])
        self.assertEqual({entry["objectId"] for entry in meta["instances"] if entry["assembly"] == "Gate"},
                         {12005, 13003})

    def test_approved_road(self):
        project = route_project()
        with self.assertRaisesRegex(ValueError, "approved road"):
            rotate_assembly(project, "Pavement", (0, 0, 0), 90)
        instance(project, "road-before")["transform"]["translation"]["x"] = 100
        with self.assertRaisesRegex(ValueError, "road-before"):
            game_records(project)

    def test_asset_card_identifies_the_house(self):
        index = load_json(ROOT / "catalog/texture_id_index.json")
        card = asset_card(10247, load_json(PROFILE)["objects"], index)
        self.assertEqual(card["category"], "SmallHouse")
        self.assertIn("DoorFrame", card["first_mesh_name"])
        self.assertGreater(min(card["dimensions_m"]), 20)

    def test_real_gate_uses_renderable_meshes(self):
        archive = Path(os.environ.get("UGC_TEST_MESHES", MESHES))
        if not archive.is_file() or archive.stat().st_size < 200:
            self.skipTest("Provide UGC_TEST_MESHES or Git LFS mesh archive")
        with MeshLibrary(archive) as library:
            for oid in {entry["objectId"] for entry in route_project()["instances"]}:
                self.assertGreater(len(library.geometry(oid).triangles), 0)


if __name__ == "__main__":
    unittest.main()
