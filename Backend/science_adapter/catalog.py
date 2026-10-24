"""Exact NISAR pair discovery; independent implementation of audited ASF contracts.

See docs/MOSHIOUR_INTEGRATION_AUDIT.md for reference revisions and policy differences.
No authenticated download URL or credential is serialized into discovery evidence.
"""
from __future__ import annotations

import math
import re
import time
from datetime import date, datetime
from typing import Any
from urllib.parse import urlsplit

import requests

ASF_API = "https://api.daac.asf.alaska.edu/services/search/param"
DATA_HOST = "nisar.asf.earthdatacloud.nasa.gov"
CATALOG_LIMIT = 2000
MAX_PRODUCTS = 32
SECONDARY = "secondary-acquisition-in-window"
BOTH = "both-acquisitions-in-window"
VARIANT_POLICY = "one release per exact geometry, acquisition pair, processing type and coverage; prefer PROVISIONAL, P-series, newest CRID, orbit accuracy and version; PR/UR/OD remain distinct"


class CatalogError(RuntimeError):
    pass


def parse_identity(value: str) -> dict[str, Any]:
    name = value.removesuffix(".h5")
    if not re.fullmatch(r"[A-Za-z0-9_]{40,250}", name):
        raise CatalogError("Invalid NISAR granule identity.")
    fields = name.split("_")
    if (len(fields) != 20 or fields[:2] != ["NISAR", "L2"]
            or fields[2] not in {"PR", "UR", "OD"} or fields[3] not in {"GUNW", "GOFF"}
            or fields[6] not in {"A", "D"} or not re.fullmatch(r"[A-Z]\d{5}", fields[15])
            or fields[16] not in {"P", "M", "N", "F"} or fields[17] not in {"F", "P"}
            or not re.fullmatch(r"\d{3}", fields[19])):
        raise CatalogError("Unsupported NISAR paired-product naming convention.")
    try:
        stamps = [datetime.strptime(x, "%Y%m%dT%H%M%S") for x in fields[11:15]]
    except ValueError:
        raise CatalogError("Invalid NISAR acquisition timestamps.") from None
    if not (stamps[0] <= stamps[1] < stamps[2] <= stamps[3]):
        raise CatalogError("NISAR reference/secondary acquisition order is invalid.")
    crid = fields[15]
    mapped = "PROVISIONAL" if crid[0] == "P" and int(crid[1:]) >= 5023 else (
        "BETA" if crid[0] == "X" and int(crid[1:]) < 5023 else "UNKNOWN")
    return {"granule": name, "productType": fields[3], "processing_type": fields[2],
            "reference": stamps[0].date(), "secondary": stamps[2].date(),
            "reference_start": fields[11], "reference_end": fields[12],
            "secondary_start": fields[13], "secondary_end": fields[14],
            "track": fields[5], "direction": fields[6], "frame": fields[7],
            "mode": fields[8:11], "crid": crid, "crid_maturity": mapped,
            "orbit_accuracy": fields[16], "coverage": fields[17], "version": fields[19]}


def normalize_payload(payload: Any, limit: int = CATALOG_LIMIT) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        if payload.get("error") or payload.get("errors"):
            raise CatalogError("ASF reported a catalog error; discovery is incomplete.")
        raw = payload.get("features", payload.get("results"))
        for key in ("next", "nextPage", "nextPageToken"):
            if payload.get(key):
                raise CatalogError("ASF returned paginated results; discovery is incomplete.")
    else:
        raw = payload
    if not isinstance(raw, list):
        raise CatalogError("ASF returned an unsupported catalog response.")
    flat = [row for entry in raw for row in (entry if isinstance(entry, list) else [entry])]
    if len(flat) >= limit:
        raise CatalogError("ASF result cap reached; narrow the AOI/date window. Discovery is incomplete.")
    rows = []
    for item in flat:
        if not isinstance(item, dict):
            raise CatalogError("ASF returned an invalid catalog record.")
        if item.get("type") == "Feature":
            if not isinstance(item.get("properties"), dict):
                raise CatalogError("ASF returned a feature without product metadata.")
            rows.append(dict(item["properties"]) | {"footprint": item.get("geometry")})
        else:
            rows.append(dict(item))
    if isinstance(payload, dict):
        for key in ("totalResults", "numberMatched", "total"):
            total = payload.get(key)
            if isinstance(total, (int, float)) and total > len(rows):
                raise CatalogError("ASF returned fewer rows than its catalog total; discovery is incomplete.")
    return rows


def in_window(record: dict[str, Any], start: date, end: date, mode: str = SECONDARY) -> bool:
    if end < start or (end - start).days > 90:
        raise CatalogError("Date window must be ordered and no longer than 90 days.")
    if mode not in {SECONDARY, BOTH}:
        raise CatalogError("Unsupported acquisition-window policy.")
    return start <= record["secondary"] <= end and (mode == SECONDARY or start <= record["reference"] <= end)


def normalize_record(row: dict[str, Any], family: str, maturity: str) -> dict[str, Any]:
    name = next((row[k] for k in ("granuleName", "fileName", "sceneName", "productName") if isinstance(row.get(k), str)), "")
    identity = parse_identity(name)
    if identity["productType"] != family or row.get("processingLevel", row.get("productType", family)) != family:
        raise CatalogError("ASF product family conflicts with the exact granule identity.")
    collection = str(row.get("collectionName") or row.get("collection") or "")
    declarations = {maturity}
    explicit = row.get("dataMaturity", row.get("maturity"))
    if explicit:
        declarations.add(str(explicit).upper())
    for label in ("BETA", "PROVISIONAL"):
        if label in collection.upper():
            declarations.add(label)
    if identity["crid_maturity"] != "UNKNOWN":
        declarations.add(identity["crid_maturity"])
    if len(declarations) != 1 or maturity not in {"BETA", "PROVISIONAL"}:
        raise CatalogError("ASF maturity conflicts with collection or recognized CRID metadata.")
    granule = identity["granule"]
    url = row.get("downloadUrl") or row.get("url")
    if url is not None:
        parsed = urlsplit(str(url))
        if (parsed.scheme != "https" or parsed.hostname != DATA_HOST or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path.rsplit("/", 1)[-1] != granule + ".h5"):
            raise CatalogError("ASF download URL does not identify the exact approved NISAR product.")
    else:
        url = f"https://{DATA_HOST}/NISAR/NISAR_L2_{family}_{maturity}_V1/{granule}/{granule}.h5"
    size = row.get("sizeBytes")
    byte_map = row.get("bytes") or row.get("sizeMB")
    if isinstance(byte_map, dict):
        entry = byte_map.get(granule + ".h5")
        if isinstance(entry, dict):
            size = entry.get("bytes")
    if not isinstance(size, (int, float)):
        size_mb = row.get("sizeMB")
        size = size_mb * 1024 * 1024 if isinstance(size_mb, (int, float)) else None
    valid_size = isinstance(size, (int, float)) and math.isfinite(size) and size > 0
    return identity | {"maturity": maturity, "url": str(url),
                       "size_mb": size / (1024 * 1024) if valid_size else None,
                       "catalog_evidence": {"maturity_filter": maturity, "collection": collection,
                                            "crid_maturity": identity["crid_maturity"],
                                            "footprint": row.get("footprint"),
                                            "processing_date": row.get("processingDate")},
                       "warnings": ["CRID_UNMAPPED_USING_ASF_MATURITY_FILTER"] if identity["crid_maturity"] == "UNKNOWN" else []}


def deduplicate(records: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    groups: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for record in records:
        parts = record["granule"].split("_")
        # PR, UR and OD are deliberately distinct. Coverage/mode are not interchangeable.
        key = tuple(parts[:15] + parts[17:19])
        groups.setdefault(key, []).append(record)
    selected, excluded = [], []
    for variants in groups.values():
        def rank(r: dict[str, Any]) -> tuple:
            return (0 if r["maturity"] == "PROVISIONAL" else 1, 0 if r["crid"][0] == "P" else 1,
                    -int(r["crid"][1:]), "PMNF".index(r["orbit_accuracy"]), -int(r["version"]), r["granule"])
        ordered = sorted(variants, key=rank)
        winner = ordered[0]
        selected.append(winner)
        excluded.extend({"granule": r["granule"], "selected_granule": winner["granule"],
                         "reason": "SUPERSEDED_PROCESSING_RELEASE"} for r in ordered[1:])
    return sorted(selected, key=lambda r: (r["secondary"], r["reference"], r["granule"])), sorted(excluded, key=lambda r: r["granule"])


def discover(aoi_wkt: str, start: date, end: date, *, product_type: str = "BOTH", acquisition_window: str = SECONDARY,
             processing_profile: str = "validation-pilot", max_products: int = MAX_PRODUCTS,
             session: Any = None, sleep: Any = time.sleep) -> dict[str, Any]:
    from .geometry import validate_wkt
    validate_wkt(aoi_wkt)
    if product_type not in {"BOTH", "GUNW", "GOFF"} or processing_profile not in {"validation-pilot", "full-aoi"}:
        raise CatalogError("Unsupported product family or processing profile.")
    in_window({"reference": start, "secondary": end}, start, end, acquisition_window)
    client = session or requests.Session()
    families = ["GUNW", "GOFF"] if product_type == "BOTH" else [product_type]
    records, excluded, queries = [], [], []
    identities: dict[str, dict[str, Any]] = {}
    for family in families:
        for maturity in ("PROVISIONAL", "BETA"):
            params = {"dataset": "NISAR", "processingLevel": family, "dataMaturity": maturity,
                      "intersectsWith": aoi_wkt, "start": "2024-01-01T00:00:00Z",
                      "end": f"{end.isoformat()}T23:59:59Z", "output": "geojson", "maxResults": CATALOG_LIMIT}
            rows = None
            for attempt in range(3):
                try:
                    response = client.get(ASF_API, params=params, timeout=(20, 120))
                    try:
                        response.raise_for_status()
                        rows = normalize_payload(response.json())
                    finally:
                        response.close()
                    break
                except (requests.RequestException, ValueError):
                    if attempt == 2:
                        raise CatalogError("ASF catalog query failed after three attempts; discovery is incomplete.") from None
                    sleep(2 ** attempt)
            queries.append({"family": family, "maturity": maturity, "count": len(rows), "parameters": params})
            for row in rows:
                record = normalize_record(row, family, maturity)
                prior = identities.get(record["granule"])
                if prior:
                    if prior["maturity"] != maturity:
                        raise CatalogError("The same granule appeared in conflicting maturity collections.")
                    continue
                identities[record["granule"]] = record
                if in_window(record, start, end, acquisition_window):
                    records.append(record)
                else:
                    excluded.append({"granule": record["granule"], "reason": "OUTSIDE_REQUESTED_ACQUISITION_WINDOW"})
    selected, variants = deduplicate(records)
    eligible_count = len(selected)
    if processing_profile == "validation-pilot":
        chosen = {max((r for r in selected if r["productType"] == f), key=lambda r: (r["secondary"], r["granule"]))["granule"]
                  for f in families if any(r["productType"] == f for r in selected)}
        excluded.extend({"granule": r["granule"], "reason": "OUTSIDE_BOUNDED_VALIDATION_PILOT"} for r in selected if r["granule"] not in chosen)
        selected = [r for r in selected if r["granule"] in chosen]
    if len(selected) > max_products:
        raise CatalogError(f"Selected product count exceeds {max_products}; narrow the AOI/date window.")
    missing = [f for f in families if not any(r["productType"] == f for r in selected)]
    return {"complete": True, "queries": queries, "selected": selected,
            "eligible_product_count": eligible_count, "excluded": excluded + variants,
            "variant_policy": VARIANT_POLICY, "unavailable_families": missing,
            "warnings": [f"NO_ELIGIBLE_{f}_PRODUCTS" for f in missing]}
