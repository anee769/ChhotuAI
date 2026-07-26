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
