"""Tenant-local knowledge graph primitives for Chhotu's learned vocabulary.

The graph stores relationships, not ledger facts. Stock, money, bills and
credit continue to come only from their transactional tables. A graph edge is
therefore safe to use for ranking or clarification, never as proof that a
financial operation happened.
"""
from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalise_label(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip()).casefold()


def entity(kind: str, canonical: str, *, ref_id: str = "",
           attributes: dict | None = None) -> dict:
    kind = normalise_label(kind)
    label = normalise_label(canonical)
    reference = str(ref_id or "").strip()
    signature = f"{kind}\0{reference or label}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
    return {
        "entity_id": f"ke_{digest}",
        "kind": kind,
        "canonical": label,
        "ref_id": reference,
        "attributes": dict(attributes or {}),
    }


def edge_id(source_id: str, relation: str, target_id: str) -> str:
    signature = f"{source_id}\0{normalise_label(relation)}\0{target_id}"
    digest = hashlib.sha256(signature.encode("utf-8")).hexdigest()[:20]
    return f"kr_{digest}"


def reinforce(graph: dict, source: dict, relation: str, target: dict, *,
              evidence: dict | None = None, confidence: float = 0.65,
              observed_at: str | None = None) -> dict:
    """Idempotently add entities and strengthen one directed relationship."""
    graph.setdefault("entities", [])
    graph.setdefault("edges", [])
    observed_at = observed_at or _now()
    relation = normalise_label(relation).replace(" ", "_")
    confidence = max(0.0, min(0.99, float(confidence)))

    for incoming in (source, target):
        existing = next(
            (row for row in graph["entities"]
             if row.get("entity_id") == incoming["entity_id"]), None)
        if existing:
            existing["canonical"] = incoming["canonical"]
            existing["attributes"] = {
                **(existing.get("attributes") or {}),
                **(incoming.get("attributes") or {}),
            }
            existing["updated_at"] = observed_at
        else:
            graph["entities"].append({
                **incoming, "created_at": observed_at,
                "updated_at": observed_at,
            })

    eid = edge_id(source["entity_id"], relation, target["entity_id"])
    row = next((item for item in graph["edges"]
                if item.get("edge_id") == eid), None)
    if row:
        count = int(row.get("evidence_count") or 1) + 1
        # Repeated independent confirmations increase trust slowly and never
        # turn a learned convention into absolute truth.
        row["confidence"] = round(min(
            0.99, max(float(row.get("confidence") or 0), confidence)
            + (1 - max(float(row.get("confidence") or 0), confidence)) * 0.12
        ), 4)
        row["evidence_count"] = count
        row["last_confirmed_at"] = observed_at
    else:
        row = {
            "edge_id": eid,
            "source_entity_id": source["entity_id"],
            "relation": relation,
            "target_entity_id": target["entity_id"],
            "confidence": confidence,
            "evidence_count": 1,
            "first_seen_at": observed_at,
            "last_confirmed_at": observed_at,
            "status": "active",
            "evidence": [],
        }
        graph["edges"].append(row)
    if evidence:
        row.setdefault("evidence", []).append(dict(evidence))
        row["evidence"] = row["evidence"][-20:]
    return dict(row)


def record_product_confirmation(repo, spoken: str, sku: dict, *,
                                unit: str = "", was_tap: bool = False) -> None:
    """Mirror an accepted product resolution into structured relationships."""
    phrase = normalise_label(spoken)
    if not phrase or not sku:
        return
    product = entity(
        "product", sku.get("canonical") or sku["sku_id"],
        ref_id=sku["sku_id"],
        attributes={"family": sku.get("family")},
    )
    term = entity("term", phrase, ref_id=f"term:{phrase}")
    repo.reinforce_knowledge(
        term, "alias_for", product,
        confidence=0.82 if was_tap else 0.72,
        evidence={"kind": "product_confirmation", "was_tap": bool(was_tap)},
    )
    family = normalise_label(sku.get("family") or "")
    if family:
        repo.reinforce_knowledge(
            product, "belongs_to", entity("product_family", family,
                                           ref_id=f"family:{family}"),
            confidence=0.95,
            evidence={"kind": "catalogue_attribute"},
        )
    if unit:
        clean_unit = normalise_label(unit)
        repo.reinforce_knowledge(
            product, "uses_unit",
            entity("unit", clean_unit, ref_id=f"unit:{clean_unit}"),
            confidence=0.8,
            evidence={"kind": "confirmed_unit"},
        )
