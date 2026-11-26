"""Versioned conversion of Toolkit 1.x authoring projects to native game space."""
from copy import deepcopy

from .game_space import is_native
from .ugc_project import game_records, native_project_from_map


def migrate(project):
    raw = game_records(project)
    if is_native(project):
        return deepcopy(project)
    excluded = {"schema", "layout_version", "coordinate_space", "origin_x_cm", "instances", "game", "approved_road"}
    receipt = {"instances": [{k: v for k, v in row.items() if k not in {"transform", "game_original"}}
                             for row in project["instances"]],
               "approved_road": project.get("approved_road", {})}
    output = {**deepcopy({k: v for k, v in project.items() if k not in excluded}),
              **native_project_from_map(raw, receipt)}
    if any(key in output for key in ("road_joins", "viewpoints", "landmarks", "source_to_authoring_m")):
        output["annotation_frame"] = {"coordinate_space": "legacy-authoring-xyz-m",
                                      "origin_x_cm": project["origin_x_cm"]}
    return output


def annotation_origin(project, default):
    return project.get("annotation_frame", {}).get("origin_x_cm", project.get("origin_x_cm", default))
