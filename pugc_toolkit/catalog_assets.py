"""Search whole catalog entries and report the evidence behind each asset card."""
from math import hypot
import re

from .ugc_validation import enterable_building

ALIASES = {
    "стена": "wall", "стены": "wall", "walls": "wall",
    "дом": "house", "здание": "house", "houses": "house",
    "скамья": "bench", "скамейка": "bench", "benches": "bench",
    "лестница": "stair", "stairs": "stair",
    "пол": "floor", "забор": "fence", "дверь": "door", "башня": "tower",
}


def _tokens(value):
    words = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value)
    return re.findall(r"[^\W_]+", words.casefold())


def _geometry(object_id, record, library):
    if library is not None and object_id in library.ids:
        mesh = library.load(object_id, 1)[0]
        return mesh["dimensions"], "complete_mesh", mesh["materials"]
    dimensions = record.get("dimensions_m")
    return dimensions, "catalog_sample" if dimensions is not None else None, record.get("mesh_materials", [])


def _materials(names, candidates, bindings):
    rows = []
    for name in names:
        candidate = candidates.get(name, {})
        path = bindings.get(name)
        guess = candidate.get("diffuse_guess")
        rows.append({
            "material": name,
            "texture": path or guess,
            "binding": "confirmed" if path else "name_similarity" if guess else "missing_binding",
            "confidence": None if path else candidate.get("confidence"),
        })
    return rows


def asset_card(object_id, profiles, index, library=None, *, profile=None, materials=None, bindings=None,
               geometry=None):
    oid = str(object_id)
    if oid not in profiles and oid not in index:
        raise KeyError(f"Unknown asset {oid}")
    card, mesh = profiles.get(oid, {}), index.get(oid, {})
    profile = profile or {}
    candidates = {row["source_material"]: row for row in (materials or {}).get(oid, [])}
    dimensions, dimensions_source, names = geometry if geometry is not None else _geometry(object_id, mesh, library)
    official_thumbnail = card.get("thumbnail")
    return {
        **card, "objectId": object_id, "category": card.get("category"), "weight": card.get("weight"),
        "profile_id": profile.get("id"), "profile_revision": profile.get("revision"),
        "profile_repository": profile.get("sources", {}).get("repository"),
        "placement_status": "listed" if oid in profiles else "not_in_profile",
        "prefer_native_scale": enterable_building(card),
        "geometry_indexed": oid in index, "mesh_available": None if library is None else object_id in library.ids,
        "dimensions_m": dimensions, "dimensions_source": dimensions_source,
        "thumbnail": official_thumbnail or mesh.get("thumbnail"),
        "thumbnail_source": ("editor_catalog" if official_thumbnail else
                             "name_similarity" if mesh.get("thumbnail") else None),
        "thumbnail_match_score": None if official_thumbnail else mesh.get("thumbnail_match_score"),
        "first_mesh_name": mesh.get("name"),
        "materials": _materials(names, candidates, bindings or {}),
    }


def _match(oid, tokens, profiles, index):
    if not tokens:
        return 0
    card = profiles.get(oid)
    if card is None:
        name = " ".join(_tokens(index[oid].get("name", "")))
        return 2 if all(token in name for token in tokens) else None
    category = _tokens(card.get("category") or "")
    if all(token in category for token in tokens):
        return 0
    target = " ".join(category + _tokens(card.get("name") or "") + _tokens(card.get("type") or ""))
    return 1 if all(token in target for token in tokens) else None


def search_catalog(purpose, dims, limit, profiles, index, library=None, *, tolerance=None, max_weight=None, **card_options):
    raw = _tokens(purpose)
    unsupported = sorted({token for token in raw if not token.isascii() and token not in ALIASES})
    if unsupported or (not raw and purpose.strip()):
        return {"query_status": "unsupported_term", "unsupported_terms": unsupported, "results": []}
    tokens = [ALIASES.get(token, token) for token in raw]
    ids = profiles.keys() | index.keys()
    exact_id = len(tokens) == 1 and tokens[0].isdecimal()
    rows = []
    for oid in ids:
        object_id = int(oid)
        weight = profiles.get(oid, {}).get("weight")
        if max_weight is not None and (weight is None or weight > max_weight):
            continue
        if exact_id:
            if oid.lstrip("0") != tokens[0].lstrip("0"):
                continue
            rank = 0
        else:
            rank = _match(oid, tokens, profiles, index)
            if rank is None:
                continue
        error, geometry = 0, None
        if dims is not None:
            geometry = _geometry(object_id, index.get(oid, {}), library)
            size = geometry[0]
            if size is None:
                continue
            distances = [abs(actual - wanted) for actual, wanted in zip(size, dims, strict=True)]
            if tolerance is not None and max(distances) > tolerance:
                continue
            error = hypot(*(delta / max(wanted, .1) for delta, wanted in zip(distances, dims, strict=True)))
        rows.append((error, rank, object_id, geometry))
    results = [
        asset_card(oid, profiles, index, library, geometry=geometry, **card_options)
        for _, _, oid, geometry in sorted(rows, key=lambda row: row[:3])[:limit]
    ]
    return {"query_status": "empty" if not tokens else "matched" if results else "no_matches",
            "unsupported_terms": [], "results": results}


def search_assets(purpose, dims, limit, profiles, index, library=None):
    """Return only the cards for callers of the previous Python API."""
    return search_catalog(purpose, dims, limit, profiles, index, library)["results"]


def coverage(profiles, index, materials, library):
    indexed, profiled, candidates = (set(map(int, data)) for data in (index, profiles, materials))
    return {
        "index_only_ids": sorted(indexed - profiled),
        "profile_only_ids": sorted(profiled - indexed),
        "unrecognized_material_ids": sorted(candidates - (indexed | profiled)),
        "unrecognized_archive_ids": sorted(library.ids - (indexed | profiled)) if library is not None else None,
    }
