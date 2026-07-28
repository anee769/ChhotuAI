"""Regression tests for Sarvam request settings."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

import sarvam_client


class TextToSpeechTests(unittest.TestCase):
    def test_long_speech_has_enough_generation_time(self):
        with patch.object(sarvam_client, "_request",
                          return_value={"audios": ["audio"]}) as request:
            audio = sarvam_client.text_to_speech(
                "A full daily summary with several ledger figures.")

        self.assertEqual(audio, "audio")
        self.assertEqual(request.call_args.kwargs["timeout"], 30)
        self.assertEqual(request.call_args.kwargs["max_retries"], 2)


if __name__ == "__main__":
    unittest.main()
