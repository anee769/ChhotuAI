"""
learning.py — every tap teaches the system (spec Section 8 & 9.5).

A confirmation (auto-accept above gate, or a human tap below it) writes:
  - a learned alias (spoken phrase -> chosen SKU)
  - a correction record (with the rejected candidates, for Stage-4 few-shot)
  - a unit prior (if a unit was resolved)
  - an attribute prior (nudging future disambiguation)

Writes go through Repo.upsert_learning so persistence stays swappable.
"""
from __future__ import annotations

from datetime import datetime
import knowledge_graph as KG


def merge_learning(current: dict, patch: dict) -> dict:
    """Merge observations into a compact shop memory.

    Aliases are one current decision per spoken phrase, priors gain weight
    instead of being duplicated, and only the most recent corrections are
    retained for the matcher's few-shot context.
    """
    current.setdefault("aliases_learned", [])
    current.setdefault("attribute_priors", [])
    current.setdefault("unit_priors", [])
    current.setdefault("corrections", [])

    for incoming in patch.get("aliases_learned", []):
        phrase = str(incoming.get("phrase") or "").strip().casefold()
        sku_id = incoming.get("sku_id")
        if not phrase or not sku_id:
            continue
        old = next((row for row in current["aliases_learned"]
                    if str(row.get("phrase") or "").strip().casefold() == phrase),
                   None)
        count = int(old.get("count") or 1) + 1 if (
            old and old.get("sku_id") == sku_id) else 1
        current["aliases_learned"] = [
            row for row in current["aliases_learned"]
            if str(row.get("phrase") or "").strip().casefold() != phrase
        ]
        current["aliases_learned"].append({
            **incoming, "phrase": phrase, "sku_id": sku_id, "count": count,
        })

    for key, identity in (
        ("attribute_priors", ("family", "attribute", "value")),
        ("unit_priors", ("sku_id", "unit")),
    ):
        for incoming in patch.get(key, []):
            old = next((row for row in current[key]
                        if all(row.get(field) == incoming.get(field)
                               for field in identity)), None)
            if old:
                old["count"] = int(old.get("count") or 0) + int(
                    incoming.get("count") or 1)
            else:
                current[key].append(dict(incoming))

    current["corrections"].extend(patch.get("corrections", []))
    current["corrections"] = current["corrections"][-100:]
    return current


def merge_seed_learning(current: dict, seed: dict) -> dict:
    """Add missing demo memories without inflating counts on migration reruns."""
    current.setdefault("aliases_learned", [])
    current.setdefault("attribute_priors", [])
    current.setdefault("unit_priors", [])
    current.setdefault("corrections", [])
    for incoming in seed.get("aliases_learned", []):
        phrase = str(incoming.get("phrase") or "").strip().casefold()
        if phrase and not any(
                str(row.get("phrase") or "").strip().casefold() == phrase
                for row in current["aliases_learned"]):
            current["aliases_learned"].append({**incoming, "phrase": phrase})
    for key, identity in (
        ("attribute_priors", ("family", "attribute", "value")),
        ("unit_priors", ("sku_id", "unit")),
    ):
        for incoming in seed.get(key, []):
            if not any(all(row.get(field) == incoming.get(field)
                           for field in identity) for row in current[key]):
                current[key].append(dict(incoming))
    for incoming in seed.get("corrections", []):
        signature = (incoming.get("spoken"), incoming.get("chosen_sku"))
        if not any((row.get("spoken"), row.get("chosen_sku")) == signature
                   for row in current["corrections"]):
            current["corrections"].append(dict(incoming))
    current["corrections"] = current["corrections"][-100:]
    return current


def record_confirmation(repo, spoken: str, chosen_sku: str, *,
                        rejected: list = None, unit: str = None,
                        was_tap: bool = False) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    patch: dict = {}
    spoken = (spoken or "").strip().lower()

    if spoken and chosen_sku:
        # learn the alias only if it isn't already a trivial/duplicate
        patch["aliases_learned"] = [
            {"phrase": spoken, "sku_id": chosen_sku, "confirmed_at": now}
        ]

    if was_tap and spoken:
        patch["corrections"] = [
            {"spoken": spoken, "chosen_sku": chosen_sku,
             "rejected": rejected or [], "ts": now}
        ]

    if unit and chosen_sku:
        patch["unit_priors"] = [{"sku_id": chosen_sku, "unit": unit, "count": 1}]

    sku = repo.sku(chosen_sku)
    if sku and sku.get("attributes"):
        priors = []
        fam = sku.get("family")
        for attr, val in sku["attributes"].items():
            if attr in ("diameter_mm", "grade", "brand", "type", "class", "size_inch"):
                priors.append({"family": fam, "attribute": attr,
                               "value": val, "count": 1})
        if priors:
            patch["attribute_priors"] = priors

    if patch:
        repo.upsert_learning(patch)
    # The graph is an additive learning index. A missing graph migration must
    # never block the existing alias memory or a confirmed ledger operation.
    if spoken and sku and hasattr(repo, "reinforce_knowledge"):
        try:
            KG.record_product_confirmation(
                repo, spoken, sku, unit=unit or "", was_tap=was_tap)
        except Exception as exc:
            print(f"[knowledge-graph] confirmation mirror failed: "
                  f"{type(exc).__name__}", flush=True)
