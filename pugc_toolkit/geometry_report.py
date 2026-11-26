"""Coverage and exit codes shared by the route audit and frame comparison."""
from . import cli_protocol as cli


def check(expected, checked, missing=(), *, findings=0, required=False, requested_item=None, skipped=False):
    missing = list(missing)
    if required and not expected:
        expected = 1
        missing.append(requested_item)
    if missing:
        status = "incomplete"
    elif findings:
        status = "mismatch"
    elif skipped or not expected:
        status = "skipped"
    else:
        status = "checked"
    return {"status": status, "expected": expected, "checked": checked, "missing": missing, "findings": findings}


def summary(strict, **checks):
    states = {row["status"] for row in checks.values()}
    status = ("partial" if "incomplete" in states else "findings" if "mismatch" in states
              else "complete" if "checked" in states else "skipped")
    return {"schema": 1, "strict": strict, "status": status, "checks": checks}


def completion(result):
    return {"partial": cli.RESOURCE, "findings": cli.FINDINGS, "complete": 0, "skipped": 0}[result["status"]]


def partial_diagnostics(result, path):
    return [
        cli.diagnostic("geometry_incomplete", f"Incomplete geometry check: {name}.",
                       "Review the missing IDs in the report and supply the required input.",
                       path=path, field=f"geometry_check.checks.{name}.missing")
        for name, row in result["checks"].items() if row["status"] == "incomplete" and name != "instances"
    ]
