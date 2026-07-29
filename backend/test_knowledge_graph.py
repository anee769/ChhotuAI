from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import knowledge_graph as KG
import learning
from repo import JsonRepo


class KnowledgeGraphTests(unittest.TestCase):
    def test_repeated_evidence_reinforces_one_edge(self):
        graph = {}
        term = KG.entity("term", "laal wali cement")
        product = KG.entity("product", "UltraTech PPC Cement 50kg",
                            ref_id="CEM_PPC")
        first = KG.reinforce(
            graph, term, "alias_for", product,
            evidence={"kind": "confirmation"}, confidence=0.72)
        second = KG.reinforce(
            graph, term, "alias_for", product,
            evidence={"kind": "confirmation"}, confidence=0.72)

        self.assertEqual(len(graph["entities"]), 2)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertEqual(second["evidence_count"], 2)
        self.assertGreater(second["confidence"], first["confidence"])

    def test_product_confirmation_builds_alias_family_and_unit_edges(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = JsonRepo(Path(directory))
            sku = {
                "sku_id": "CEM_PPC",
                "canonical": "UltraTech PPC Cement 50kg",
                "family": "cement",
            }
            KG.record_product_confirmation(
                repo, "laal wali cement", sku, unit="bori", was_tap=True)
            graph = repo.load_knowledge_graph()

        self.assertEqual(
            {edge["relation"] for edge in graph["edges"]},
            {"alias_for", "belongs_to", "uses_unit"},
        )
        alias = next(edge for edge in graph["edges"]
                     if edge["relation"] == "alias_for")
        self.assertEqual(alias["evidence_count"], 1)
        self.assertEqual(alias["confidence"], 0.82)

    def test_existing_learning_confirmation_is_mirrored_into_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = JsonRepo(Path(directory))
            repo.upsert_sku({
                "sku_id": "CEM_PPC",
                "canonical": "UltraTech PPC Cement 50kg",
                "family": "cement",
                "attributes": {"brand": "UltraTech", "type": "PPC"},
                "default_unit": "bori",
                "units": {"bori": 1},
                "aliases": [],
            })
            learning.record_confirmation(
                repo, "laal wali cement", "CEM_PPC",
                unit="bori", was_tap=True)
            graph = repo.load_knowledge_graph()
            legacy = repo.load_learning()

        self.assertEqual(legacy["aliases_learned"][0]["sku_id"], "CEM_PPC")
        self.assertIn("alias_for",
                      {edge["relation"] for edge in graph["edges"]})

    def test_legacy_aliases_are_backfilled_once_into_graph(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = JsonRepo(Path(directory))
            repo.upsert_sku({
                "sku_id": "TMT_12",
                "canonical": "Tata Tiscon TMT Bar 12mm Fe500D",
                "family": "tmt", "attributes": {"diameter_mm": 12},
                "default_unit": "tonne", "units": {"tonne": 1000},
                "aliases": [],
            })
            repo.seed_learning({
                "aliases_learned": [
                    {"phrase": "patla sariya", "sku_id": "TMT_12", "count": 9}
                ],
                "unit_priors": [
                    {"sku_id": "TMT_12", "unit": "tonne", "count": 5}
                ],
            })
            self.assertEqual(learning.backfill_knowledge_graph(repo), 3)
            self.assertEqual(learning.backfill_knowledge_graph(repo), 0)
            edges = repo.load_knowledge_graph()["edges"]
            aliases = [edge for edge in edges
                       if edge["relation"] == "alias_for"]

        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["evidence_count"], 1)
        self.assertEqual(
            {edge["relation"] for edge in edges},
            {"alias_for", "belongs_to", "uses_unit"},
        )


if __name__ == "__main__":
    unittest.main()
