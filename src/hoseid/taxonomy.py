"""Mapping SpeciesNet taxonomy strings to display labels, and assigning review priority.

The mapping is a config file (config/taxon_map.json), not code, and `taxon_raw` is always
retained on the detection record -- so a mapping change is a cheap re-map rather than a re-run
of inference.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "taxon_map.json"


@dataclass(frozen=True)
class TaxonResult:
    taxon: str
    taxon_raw: str
    review_priority: str


@lru_cache(maxsize=4)
def _load(path_str: str) -> dict:
    return json.loads(Path(path_str).read_text())


def map_version(config_path: Path | None = None) -> str:
    return str(_load(str(config_path or DEFAULT_CONFIG)).get("version", "0"))


def _keys_of(taxon_raw: str) -> list[str]:
    """Candidate lookup keys for a SpeciesNet label, most specific first.

    Labels are `uuid;class;order;family;genus;species;common`, but the geofence roll-up
    deliberately produces *less* specific labels when a species is not plausible in the region:
    a white-tailed deer in California comes back as `...;cervidae;;;cervidae family` with genus
    and species blank. Those rollups are the normal case under an active geofence, not an edge
    case, so a genus;species-only key would miss most geofenced output.

    We therefore try genus;species, then family, then order.
    """
    parts = [p.strip().lower() for p in taxon_raw.split(";")]
    if len(parts) < 7:
        return [taxon_raw.strip().lower()]
    _, cls, order, family, genus, species, _common = parts[:7]
    keys: list[str] = []
    if genus or species:
        keys.append(f"{genus};{species}")
    if family:
        keys.append(family)
    if order:
        keys.append(order)
    if not keys:
        keys.append(f";{parts[-1]}")          # e.g. ';blank', ';vehicle'
    return keys


def map_taxon(taxon_raw: str, confidence: float | None,
              config_path: Path | None = None) -> TaxonResult:
    cfg = _load(str(config_path or DEFAULT_CONFIG))
    taxon = None
    for key in _keys_of(taxon_raw or ""):
        entry = cfg["map"].get(key)
        if entry:
            taxon = entry["taxon"]
            break

    if taxon is None:
        low = (taxon_raw or "").lower()
        for rule in cfg["fallbacks"]["rules"]:
            if rule["contains"] in low:
                taxon = rule["taxon"]
                break
        else:
            taxon = cfg["fallbacks"]["default"]

    rp = cfg["review_priority"]
    if taxon in rp["always_high"]:
        priority = "high"
    elif taxon.startswith("unknown"):
        priority = rp.get("unknown_taxa_priority", "high")
    elif confidence is not None and confidence < rp["low_confidence_threshold"]:
        priority = "high"
    else:
        priority = "normal"

    return TaxonResult(taxon=taxon, taxon_raw=taxon_raw, review_priority=priority)
