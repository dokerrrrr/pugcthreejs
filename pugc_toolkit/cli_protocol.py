"""Versioned responses shared by the CLI and its Blender subprocess."""
import argparse
import ast
import json
from pathlib import Path
import re
import struct
from zipfile import BadZipFile, is_zipfile

from .pugc_codec import OpenSSLUnavailable, PugcError
from .ugc_storage import PublicationError

INVALID = 1
FINDINGS = 2
RESOURCE = 4
FAILED = 5
USAGE = 64
SCHEMA_VERSION = 1
INPUT_ERRORS = (OSError, ValueError, LookupError, TypeError, OverflowError, BadZipFile, struct.error)
_QUOTED_ID = r"""(?:'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")"""
_VALIDATION_LOCATION = re.compile(
    rf"^(?P<field>[\w.\[\]]+)(?: \((?P<instance>{_QUOTED_ID})\)(?P<suffix>[\w.\[\]]*)"
    rf"|\[(?P<approval>{_QUOTED_ID})\])?: "
)


def diagnostic(code, message, action, *, path=None, field=None, instance_id=None, object_id=None,
               severity="error"):
    return {
        "code": code, "message": message, "action": action, "severity": severity,
        "path": str(Path(path).absolute()) if path is not None else None,
        "field": field, "instance_id": instance_id, "object_id": object_id,
    }


class CliError(ValueError):
    def __init__(self, exit_code, code, message, action, **location):
        super().__init__(message)
        self.exit_code = exit_code
        self.diagnostic = diagnostic(code, message, action, **location)


class HelpRequested(Exception):
    pass


class MachineParser(argparse.ArgumentParser):
    def error(self, message):
        raise CliError(USAGE, "invalid_arguments", message, "Run the command with --help.")

    def print_help(self, file=None):
        raise HelpRequested(self.format_help())


def response(data=None, *, artifacts=None, diagnostics=(), exit_code=0):
    def artifact_path(value):
        return str(Path(value).absolute())

    return {
        "schema": SCHEMA_VERSION,
        "status": "error" if exit_code else "ok",
        "exit_code": exit_code,
        "data": {} if data is None else data,
        "artifacts": {
            key: [artifact_path(path) for path in value] if isinstance(value, list) else artifact_path(value)
            for key, value in (artifacts or {}).items()
        },
        "diagnostics": list(diagnostics),
    }


def from_error(exc, *, path=None, code="invalid_input", artifacts=None):
    if isinstance(exc, CliError):
        error = exc
    elif isinstance(exc, OpenSSLUnavailable):
        error = CliError(RESOURCE, "openssl_missing", str(exc), "Install OpenSSL and add it to PATH.")
    elif isinstance(exc, FileNotFoundError):
        error = CliError(RESOURCE, "input_missing", str(exc), "Supply an existing input file.",
                         path=exc.filename or path)
    elif isinstance(exc, (BadZipFile, struct.error)):
        error = CliError(RESOURCE, "archive_invalid", str(exc), "Supply a verified ZIP or NPZ archive.",
                         path=path)
    elif isinstance(exc, (OSError, PublicationError)):
        error = CliError(FAILED, "file_operation_failed", str(exc),
                         "Check the path and permissions. For an incomplete build, run pugc recover.",
                         path=getattr(exc, "filename", None) or path)
    elif isinstance(exc, PugcError):
        error = CliError(INVALID, "invalid_map", str(exc), "Check the input map and its PUGC version.",
                         path=path)
    else:
        error = CliError(INVALID, code, str(exc), "Correct the input and retry.", path=path)
    return response(artifacts=artifacts, diagnostics=[error.diagnostic], exit_code=error.exit_code)


def archive_input(path, option, *, machine):
    path = Path(path)
    label = "Mesh" if option == "--meshes" else "Texture"
    if not path.is_file():
        raise CliError(RESOURCE, "archive_missing",
                       f"{label} archive not found: {path}. Choose a local ZIP with {option}.",
                       f"Pass a local ZIP with {option}.", path=path, field=option)
    if machine:
        with path.open("rb") as stream:
            if stream.read(42).startswith(b"version https://git-lfs.github.com/spec/v1"):
                raise CliError(RESOURCE, "lfs_pointer", f"{path} is a Git LFS pointer.",
                               f"Download the Git LFS object or pass a local ZIP with {option}.",
                               path=path, field=option)
            if not is_zipfile(stream):
                raise CliError(RESOURCE, "archive_invalid", f"{path} is not a readable ZIP archive.",
                               f"Pass a valid ZIP with {option}.", path=path, field=option)


def archive_argument(args, parser, option):
    try:
        archive_input(getattr(args, option[2:]), option, machine=isinstance(parser, MachineParser))
    except CliError as exc:
        if isinstance(parser, MachineParser):
            raise
        parser.error(str(exc))


def validation_response(report, path, code, *, data=None, artifacts=None):
    from .ugc_validation import HOUSE_SCALE_WARNING

    diagnostics = []
    for key, severity in (("errors", "error"), ("warnings", "warning")):
        for message in report[key]:
            # Validator messages start with a field and can include a quoted instance ID.
            match = _VALIDATION_LOCATION.match(message)
            field, instance_id = None, None
            if match:
                field = match["field"] + (match["suffix"] or "")
                quoted_id = match["instance"] or match["approval"]
                if quoted_id:
                    try:
                        instance_id = ast.literal_eval(quoted_id)
                    except (SyntaxError, ValueError):
                        instance_id = None
                if match["approval"]:
                    field += f"[{match['approval']}]"
            house_scale = severity == "warning" and message.endswith(HOUSE_SCALE_WARNING)
            diagnostics.append(diagnostic(
                code if severity == "error" else "house_scale" if house_scale else "validation_warning", message,
                "Correct the field and run pugc validate again." if severity == "error" else
                "Use scale [1, 1, 1] to retain doorway, floor and stair dimensions." if house_scale else
                "Review the reported field and placement budget.", path=path, field=field,
                instance_id=instance_id, severity=severity,
            ))
    return response(report if data is None else data, artifacts=artifacts, diagnostics=diagnostics,
                    exit_code=0 if report["valid"] else INVALID)


def missing_mesh(path, *, instance_id=None, object_id=None):
    return diagnostic("mesh_missing", "The selected ZIP lacks a required model.",
                      "Supply an archive containing the model with --meshes.",
                      path=path, field="--meshes", instance_id=instance_id, object_id=object_id)


def write_response(result, stream):
    print(json.dumps(result, ensure_ascii=True, allow_nan=False), file=stream)
