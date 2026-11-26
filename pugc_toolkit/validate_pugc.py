"""Validate PUGC v3 data and measure placement budget by route zone."""
from __future__ import annotations

import argparse
import json

from . import cli_protocol as cli
from .pugc_codec import PugcError
from .resources import PROFILE
from .ugc_project import check_project_output, load_json, raw_with_receipt
from .ugc_storage import PublicationError
from .ugc_validation import validate


def add_arguments(parser):
    parser.add_argument("pugc", nargs="?")
    parser.add_argument("--project", help="Estimate the budget before building a PUGC")
    parser.add_argument("--profile", default=PROFILE)
    parser.add_argument("--receipt")
    parser.add_argument("--strict-scale", action="store_true",
                        help="Enforce authoring scale limits when validating a saved map")
    parser.add_argument("--allowed-ids", nargs="*", type=int)


def run(args, parser):
    if bool(args.pugc) == bool(args.project):
        parser.error("Pass a PUGC or --project")
    if args.receipt and not args.pugc:
        parser.error("--receipt requires a PUGC")
    digest = None
    machine = isinstance(parser, cli.MachineParser)
    try:
        profile = load_json(args.profile)
    except (OSError, ValueError) as exc:
        if not machine:
            raise
        return cli.from_error(exc, path=args.profile, code="invalid_profile")
    if args.project:
        try:
            project = load_json(args.project)
        except (OSError, ValueError) as exc:
            if not machine:
                raise
            return cli.from_error(exc, path=args.project, code="invalid_project")
        _, result = check_project_output(project, profile, args.allowed_ids)
    else:
        try:
            data, receipt = raw_with_receipt(args.pugc, args.receipt)
        except (OSError, ValueError) as exc:
            if machine:
                return cli.from_error(exc, path=args.pugc, code="invalid_map")
            if isinstance(exc, (PugcError, PublicationError)):
                parser.error(str(exc))
            raise
        digest = receipt["pugc_sha256"]
        zones = [entry.get("zone") for entry in receipt["instances"]]
        labels = ([f"objects[{i}] ({entry['id']!r})" for i, entry in enumerate(receipt["instances"])]
                  if "schema" in receipt else None)
        result = validate(data, profile, zones, args.allowed_ids, labels=labels, strict_scale=args.strict_scale)
    result.update(filename=args.pugc or args.project, pugc_sha256=digest)
    if machine:
        return cli.validation_response(result, result["filename"], "invalid_project" if args.project else "invalid_map")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    return run(parser.parse_args(argv), parser)


if __name__ == "__main__":
    raise SystemExit(main())
