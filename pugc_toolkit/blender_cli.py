"""Launch packaged geometry commands in Blender's Python environment."""
import argparse
from contextlib import nullcontext
from importlib import import_module
import json
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory

from . import cli_protocol as cli
from .resources import PACKAGE_ROOT

OPERATIONS = {
    "extract": "blender_extract_geometry",
    "compare": "compare_pugc",
    "audit": "audit_route",
    "preview": "blender_preview_pugc",
}


def add_arguments(parser):
    parser.add_argument("--blender", default="blender", help="Blender executable or its full path")
    parser.add_argument("--blend", type=Path, help="Open this .blend file before running the operation")
    parser.add_argument("--threads", type=int, help="Thread count for Blender")
    parser.add_argument("operation", choices=OPERATIONS)
    parser.add_argument("arguments", nargs=argparse.REMAINDER, help="Arguments passed to the geometry operation")


def run(args, parser):
    machine = isinstance(parser, cli.MachineParser)
    executable = shutil.which(args.blender)
    if executable is None:
        message = f"Blender executable not found: {args.blender}. Install Blender or pass --blender."
        if machine:
            raise cli.CliError(cli.RESOURCE, "blender_missing", message,
                               "Install Blender or pass --blender.", field="--blender")
        parser.error(message)
    command = [executable, "--background"]
    if args.blend is not None:
        if not args.blend.is_file():
            if machine:
                raise cli.CliError(cli.RESOURCE, "input_missing", f"Blender input file not found: {args.blend}",
                                   "Supply an existing .blend file.", path=args.blend, field="--blend")
            parser.error(f"Blender input file not found: {args.blend}")
        command.append(str(args.blend.resolve()))
    if args.threads is not None:
        if args.threads < 1:
            parser.error("--threads must be positive")
        command.extend(["--threads", str(args.threads)])

    operation = OPERATIONS[args.operation]
    # Load only this package so Blender keeps its own Python dependencies.
    bootstrap = (
        "import importlib.util, sys",
        "from pathlib import Path",
        "sys.dont_write_bytecode = True",
        f"package_root = Path({str(PACKAGE_ROOT)!r})",
        "spec = importlib.util.spec_from_file_location("
        "'pugc_toolkit', package_root / '__init__.py', submodule_search_locations=[str(package_root)])",
        "package = importlib.util.module_from_spec(spec)",
        "sys.modules[spec.name] = package",
        "spec.loader.exec_module(package)",
    )
    forwarded = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
    with (TemporaryDirectory() if machine else nullcontext()) as directory:
        if machine:
            result_path = Path(directory) / "result.json"
            runner = ("from pugc_toolkit.blender_cli import run_worker",
                      f"run_worker({operation!r}, {str(result_path)!r})")
        else:
            runner = (f"from pugc_toolkit.{operation} import main", "main()")
        expression = "\n".join((*bootstrap, *runner))
        command.extend(["--python-exit-code", "1", "--python-expr", expression, "--", *forwarded])
        try:
            process = subprocess.run(command, stdout=sys.stderr if machine else None)
        except OSError as exc:
            if machine:
                raise cli.CliError(cli.FAILED, "blender_failed", str(exc),
                                   "Check the executable passed to --blender.") from exc
            parser.error(f"Could not start Blender: {exc}")
        if not machine:
            return process.returncode
        if process.returncode == 0 and result_path.is_file():
            return json.loads(result_path.read_text(encoding="utf8"))
        raise cli.CliError(cli.FAILED, "blender_failed",
                           f"Blender finished without a response (process code {process.returncode}).",
                           "Check the Blender log on stderr and verify the input scene.")


def run_worker(operation, result_path):
    try:
        result = import_module(f".{operation}", __package__).main(machine=True)
    except cli.HelpRequested as exc:
        result = cli.response({"help": str(exc)})
    except ModuleNotFoundError as exc:
        if exc.name != "numpy":
            raise
        result = cli.from_error(cli.CliError(cli.RESOURCE, "numpy_missing", str(exc),
                                             "Use a Blender installation that includes NumPy."))
    except cli.INPUT_ERRORS as exc:
        result = cli.from_error(exc)
    with Path(result_path).open("w", encoding="utf8") as stream:
        cli.write_response(result, stream)
