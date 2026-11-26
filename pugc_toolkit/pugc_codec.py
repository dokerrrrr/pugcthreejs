"""PUBG PC UGC container codec. Raw APIs use game coordinates in centimeters."""
import binascii
from copy import deepcopy
import json
import pathlib
import struct
import subprocess
import zlib

CONTAINER_VERSION = 3
LAYOUT_VERSION = 2
MAX_CONTAINER_BYTES = 8 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024
_MAGIC = b".pugc"
_HEADER = struct.Struct("<5sII")
_UINT_PAIR = struct.Struct("<II")
_AES_BLOCK_BYTES = 16


class PugcError(ValueError):
    """An invalid PUGC container or an unavailable codec dependency."""


class OpenSSLUnavailable(PugcError):
    """The OpenSSL executable is absent."""


def _key(length, crc):
    product = (crc * crc) & 0xffffffff
    seed = f"{length}-{crc}-{product}-{product >> 11}".encode("ascii")
    return (seed * 32)[:32]


def _crypt(data, length, crc, *, decrypt=False):
    command = ["openssl", "enc", "-aes-256-ecb", "-K", _key(length, crc).hex(), "-nopad", "-nosalt"]
    if decrypt:
        command.insert(2, "-d")
    try:
        return subprocess.run(command, input=data, capture_output=True, check=True).stdout
    except FileNotFoundError as exc:
        raise OpenSSLUnavailable("OpenSSL is required for PUGC. Install it and add the openssl executable to PATH.") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        operation = "decrypt" if decrypt else "encrypt"
        raise PugcError(f"OpenSSL could not {operation} the PUGC payload.") from exc


def encode_raw(data):
    """Encode game-coordinate JSON into a wrapped v3 container."""
    if not isinstance(data, dict):
        raise PugcError("PUGC JSON must be an object")
    try:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8") + b"\0"
    except (TypeError, ValueError, RecursionError) as exc:
        raise PugcError("Cannot encode PUGC JSON") from exc
    if len(raw) > MAX_JSON_BYTES:
        raise PugcError("PUGC JSON exceeds the 32 MiB limit")
    compressed = zlib.compress(raw, 6)
    payload = _UINT_PAIR.pack(len(compressed), len(raw)) + compressed
    padded = payload + b"\0" * (-len(payload) % _AES_BLOCK_BYTES)
    if _HEADER.size + len(padded) + _UINT_PAIR.size > MAX_CONTAINER_BYTES:
        raise PugcError("PUGC container exceeds the 8 MiB limit")
    checksum = binascii.crc32(payload)
    encrypted = _crypt(padded, len(payload), checksum)
    header_prefix = _MAGIC + struct.pack("<I", CONTAINER_VERSION)
    header = _HEADER.pack(_MAGIC, CONTAINER_VERSION, binascii.crc32(header_prefix))
    return header + encrypted + _UINT_PAIR.pack(len(payload), checksum)


def _decode_json(payload):
    compressed_length, raw_length = _UINT_PAIR.unpack_from(payload)
    if compressed_length != len(payload) - _UINT_PAIR.size or compressed_length <= 6:
        raise PugcError("Invalid PUGC compressed length")
    if not 0 < raw_length <= MAX_JSON_BYTES:
        raise PugcError("Invalid PUGC uncompressed length (32 MiB limit)")

    inflater = zlib.decompressobj()
    try:
        raw = inflater.decompress(payload[_UINT_PAIR.size:], raw_length + 1)
    except zlib.error as exc:
        raise PugcError("Invalid PUGC zlib stream") from exc
    if len(raw) != raw_length:
        raise PugcError("PUGC uncompressed length mismatch")
    if not inflater.eof or inflater.unused_data or inflater.unconsumed_tail:
        raise PugcError("Truncated or trailing data in PUGC zlib stream")
    try:
        game = json.loads(raw.rstrip(b"\0").decode("utf-8-sig"))
    except (ValueError, RecursionError) as exc:
        raise PugcError("Invalid PUGC JSON") from exc
    if not isinstance(game, dict):
        raise PugcError("PUGC JSON must be an object")
    return game


def _decode_container(data, versions, allow_unwrapped=False):
    if len(data) > MAX_CONTAINER_BYTES:
        raise PugcError("PUGC container exceeds the 8 MiB limit")
    data = bytes(data)
    if len(data) < len(_MAGIC):
        raise PugcError("Truncated PUGC container")
    offset, version = 0, None
    if data.startswith(_MAGIC):
        if len(data) < _HEADER.size:
            raise PugcError("Truncated PUGC header")
        _, version, header_crc = _HEADER.unpack_from(data)
        if binascii.crc32(data[:9]) != header_crc:
            raise PugcError("PUGC header CRC mismatch")
        if version not in versions:
            expected = " or ".join(map(str, versions))
            raise PugcError(f"Unsupported PUGC container version {version} (expected {expected})")
        offset = _HEADER.size
    elif not allow_unwrapped:
        raise PugcError("Invalid PUGC header magic")

    encrypted_length = len(data) - offset - _UINT_PAIR.size
    if encrypted_length < _AES_BLOCK_BYTES or encrypted_length % _AES_BLOCK_BYTES:
        raise PugcError("Truncated or misaligned encrypted PUGC payload")
    length, checksum = _UINT_PAIR.unpack_from(data, len(data) - _UINT_PAIR.size)
    if length <= _UINT_PAIR.size or length + (-length % _AES_BLOCK_BYTES) != encrypted_length:
        raise PugcError("Invalid PUGC payload length in footer")
    decrypted = _crypt(data[offset:-_UINT_PAIR.size], length, checksum, decrypt=True)
    payload = decrypted[:length]
    if binascii.crc32(payload) != checksum:
        raise PugcError("PUGC payload CRC mismatch")
    if any(decrypted[length:]):
        raise PugcError("Invalid PUGC payload padding")
    return _decode_json(payload), version


def decode_raw(pugc_bytes):
    """Decode a wrapped v3 container to game-coordinate JSON."""
    return _decode_container(pugc_bytes, (CONTAINER_VERSION,))[0]


def read_pugc_bytes(path):
    """Read at most the supported container size, including for callers that hash the bytes."""
    with pathlib.Path(path).open("rb") as stream:
        data = stream.read(MAX_CONTAINER_BYTES + 1)
    if len(data) > MAX_CONTAINER_BYTES:
        raise PugcError(f"{path}: PUGC container exceeds the 8 MiB limit")
    return data


def read_game(path):
    return decode_raw(read_pugc_bytes(path))


def read_lab_game(path, *, allow_unwrapped=True):
    """Read historical game-space data for labs. Return JSON and the wrapper version, if any."""
    return _decode_container(read_pugc_bytes(path), (1, CONTAINER_VERSION), allow_unwrapped)

def convert_layout(data, origin_x_cm=50000.0):
    """Reflect world X about the datum and local X about the object's origin."""
    out = deepcopy(data)
    for item in out.get("objects", []):
        placement = item["spawnTransform"]
        position, rotation = placement["translation"], placement["rotation"]
        position["x"] = round(2 * origin_x_cm - position["x"], 8)
        rotation.update(y=-rotation["y"], z=-rotation["z"])
    return out

def encode(data, origin_x_cm=50000.0):
    return encode_raw(convert_layout(data, origin_x_cm))

def decode(pugc_bytes, origin_x_cm=50000.0):
    return convert_layout(decode_raw(pugc_bytes), origin_x_cm)

def read(path, origin_x_cm=50000.0):
    return decode(read_pugc_bytes(path), origin_x_cm)

def write(path, data, origin_x_cm=50000.0):
    pathlib.Path(path).write_bytes(encode(data, origin_x_cm))
