import binascii
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

from pugc_codec import (MAX_CONTAINER_BYTES, MAX_JSON_BYTES, PugcError, _crypt, decode_raw, encode_raw,
                        read_game, read_lab_game)


def header(version):
    prefix = b".pugc" + struct.pack("<I", version)
    return prefix + struct.pack("<I", binascii.crc32(prefix))


def wrapped_payload(payload):
    crc = binascii.crc32(payload)
    padded = payload + b"\0" * (-len(payload) % 16)
    return header(3) + _crypt(padded, len(payload), crc) + struct.pack("<II", len(payload), crc)


class ContainerTest(unittest.TestCase):
    def test_header_footer_and_crc_diagnostics(self):
        data = encode_raw({"name": "Fixture", "dataVersion": 3, "objects": []})
        for corrupted, message in (
            (data[:7], "Truncated PUGC header"),
            (data[:-1], "Truncated or misaligned"),
            (header(2) + data[13:], "container version 2"),
            (data[:9] + bytes([data[9] ^ 1]) + data[10:], "header CRC mismatch"),
            (data[:-8] + struct.pack("<I", 4) + data[-4:], "payload length in footer"),
            (data[:-1] + bytes([data[-1] ^ 1]), "payload CRC mismatch"),
        ):
            with self.subTest(message=message):
                with self.assertRaisesRegex(PugcError, message):
                    decode_raw(corrupted)

    def test_rejects_invalid_compression_metadata_and_streams(self):
        raw = b'{"dataVersion":3}\0'
        compressed = zlib.compress(raw)
        for stream, declared_compressed, declared_raw, message in (
            (compressed, len(compressed) + 1, len(raw), "compressed length"),
            (compressed, len(compressed), MAX_JSON_BYTES + 1, "32 MiB limit"),
            (compressed, len(compressed), len(raw) - 1, "uncompressed length mismatch"),
            (compressed[:-1], len(compressed) - 1, len(raw), "zlib stream"),
            (compressed + b"x", len(compressed) + 1, len(raw), "zlib stream"),
            (compressed[:-1] + bytes([compressed[-1] ^ 1]), len(compressed), len(raw), "Invalid PUGC zlib"),
        ):
            with self.subTest(message=message):
                binary = wrapped_payload(struct.pack("<II", declared_compressed, declared_raw) + stream)
                with self.assertRaisesRegex(PugcError, message):
                    decode_raw(binary)

    def test_limits_path_reads_and_describes_missing_openssl(self):
        data = encode_raw({"objects": []})
        with TemporaryDirectory() as directory:
            path = Path(directory) / "oversized.pugc"
            with path.open("wb") as stream:
                stream.truncate(MAX_CONTAINER_BYTES + 1)
            with self.assertRaisesRegex(PugcError, "8 MiB limit"):
                read_game(path)
        with patch("pugc_codec.subprocess.run", side_effect=FileNotFoundError("openssl")):
            with self.assertRaisesRegex(PugcError, "Install it.*PATH"):
                decode_raw(data)
            with self.assertRaisesRegex(PugcError, "Install it.*PATH"):
                encode_raw({"objects": []})

    def test_historical_envelopes_use_explicit_game_space_api(self):
        payload = {"dataVersion": 1, "modObjects": [], "unknown": {"keep": 1}}
        unsigned = encode_raw(payload)[13:]
        with TemporaryDirectory() as directory:
            path = Path(directory) / "historic.pugc"
            path.write_bytes(unsigned)
            self.assertEqual(read_lab_game(path), (payload, None))
            with self.assertRaisesRegex(PugcError, "header magic"):
                read_lab_game(path, allow_unwrapped=False)
            path.write_bytes(header(1) + unsigned)
            self.assertEqual(read_lab_game(path, allow_unwrapped=False), (payload, 1))
            with self.assertRaisesRegex(PugcError, "container version 1"):
                read_game(path)

    def test_corrupt_container_in_cli_reports_reason_without_creating_project(self):
        with TemporaryDirectory() as directory:
            map_path = Path(directory) / "truncated.pugc"
            project = Path(directory) / "project.json"
            map_path.write_bytes(b".pugc\3")
            cases = [
                ("ugc_project.py", ["import", map_path, project]),
                ("validate_pugc.py", [map_path]),
            ]
            for script, args in cases:
                result = subprocess.run([sys.executable, "-B", ROOT / "tools" / script, *args],
                                        capture_output=True, text=True, cwd=directory)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Truncated PUGC header", result.stderr)
                self.assertNotIn("Traceback", result.stderr)
            self.assertFalse(project.exists())


@unittest.skipUnless(os.getenv("UGC_TEST_JS_CODEC") and shutil.which("node"),
                     "Set UGC_TEST_JS_CODEC to the pinned upstream browser codec")
class NodeContainerTest(unittest.TestCase):
    def run_node(self, action, source, output):
        subprocess.run(["node", ROOT / "tools/pugc_portable.mjs", action,
                        os.environ["UGC_TEST_JS_CODEC"], source, output], check=True)

    def test_python_node_python_round_trip(self):
        raw = {"name": "Карта", "dataVersion": 3, "objects": [], "unknown": [17, {"label": "α"}]}
        with TemporaryDirectory() as directory:
            source, intermediate, output = (Path(directory) / filename for filename in
                                            ("source.pugc", "decoded.json", "encoded.pugc"))
            source.write_bytes(encode_raw(raw))
            self.run_node("decode", source, intermediate)
            self.assertEqual(json.loads(intermediate.read_bytes()), raw)
            self.run_node("encode", intermediate, output)
            self.assertEqual(read_game(output), raw)

    def test_node_encodes_game_coordinates_that_python_reads_verbatim(self):
        raw = {
            "name": "Node input", "dataVersion": 3, "objects": [{
                "objectId": 15006, "deviceIndex": -1, "userDeviceName": "",
                "spawnTransform": {
                    "translation": {"x": 49123.123456789, "y": -2789, "z": 1170},
                    "rotation": {"x": .1, "y": .2, "z": .3, "w": .9},
                    "scale3D": {"x": .8, "y": 1.25, "z": .35},
                },
            }],
        }
        with TemporaryDirectory() as directory:
            source, output = (Path(directory) / filename for filename in ("game.json", "map.pugc"))
            source.write_text(json.dumps(raw), encoding="utf-8")
            self.run_node("encode", source, output)
            self.assertEqual(read_game(output), raw)


if __name__ == "__main__":
    unittest.main()
