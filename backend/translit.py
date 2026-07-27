"""Deterministic Devanagari -> Latin transliteration for names.

Customer names are stored in Latin script so bills, the CRM table and search
read the same regardless of which language the owner spoke in.
conversation.py asks the LLM first, because it produces the spelling a person
would actually use ("Anu Devnath" rather than a letter-by-letter rendering).
But that call can fail — a timeout on a serverless host, a missing API key, a
model that answers in Devanagari anyway — and it failed by returning the input
unchanged, which is how "अनु देवनाथ" reached the customer table.

So this exists as the guarantee underneath: no network, no API key, no way to
fail. Slightly clunkier output, always Latin.
"""
from __future__ import annotations

import re
import unicodedata

_INDEPENDENT = {
    "अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u",
    "ऋ": "ri", "ॠ": "ri", "ऌ": "li", "ए": "e", "ऐ": "ai", "ओ": "o",
    "औ": "au", "ऑ": "o", "ऍ": "e",
}

_CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "ng",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "ny",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ल": "l", "व": "v", "ळ": "l",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
}

# Matras replace the consonant's inherent "a". Long vowels map to the SHORT
# Latin letter on purpose: Indian English writes Sharma and Kumar, not
# Sharmaa and Kumaar.
_MATRA = {
    "ा": "a", "ि": "i", "ी": "i", "ु": "u",
    "ू": "u", "ृ": "ri", "े": "e", "ै": "ai",
    "ो": "o", "ौ": "au", "ॉ": "o", "ॅ": "e",
}

_VIRAMA = "्"
_NUKTA = "़"
_ANUSVARA = {"ं": "n", "ँ": "n", "ः": "h"}
_DIGITS = {"०": "0", "१": "1", "२": "2", "३": "3",
           "४": "4", "५": "5", "६": "6", "७": "7",
           "८": "8", "९": "9"}
# Nukta forms that aren't a plain consonant + dot.
_NUKTA_OVERRIDE = {"ज": "z", "फ": "f", "ड": "r", "ढ": "rh", "ख": "kh",
                   "ग": "g", "क": "q"}

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ]")


def has_devanagari(text: str) -> bool:
    return bool(DEVANAGARI_RE.search(text or ""))


def to_latin(text: str) -> str:
    """Transliterate, leaving any non-Devanagari (already-Latin) text alone."""
    if not has_devanagari(text):
        return text or ""
    # Decompose so precomposed nukta letters (क़) become base + nukta and hit
    # the same code path as the typed-separately form.
    chars = list(unicodedata.normalize("NFD", text))
    out = []
    i = 0
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""

        if ch in _CONSONANTS:
            base = _CONSONANTS[ch]
            if nxt == _NUKTA:
                base = _NUKTA_OVERRIDE.get(ch, base)
                i += 1
                nxt = chars[i + 1] if i + 1 < len(chars) else ""
            out.append(base)
            if nxt in _MATRA:
                out.append(_MATRA[nxt])
                i += 2
                continue
            if nxt == _VIRAMA:
                # Virama kills the inherent vowel: this consonant clusters
                # with the next one.
                i += 2
                continue
            # Bare consonant carries an inherent "a"; a marker is used so the
            # word-final one can be dropped below without touching real a's.
            out.append("")
            i += 1
            continue

        if ch in _INDEPENDENT:
            out.append(_INDEPENDENT[ch])
        elif ch in _ANUSVARA:
            out.append(_ANUSVARA[ch])
        elif ch in _DIGITS:
            out.append(_DIGITS[ch])
        elif ch in _MATRA:
            out.append(_MATRA[ch])  # stray matra
        elif ch in (_VIRAMA, _NUKTA) or unicodedata.combining(ch):
            pass
        else:
            out.append(ch)
        i += 1

    joined = "".join(out)
    # Schwa deletion: Hindi drops the final inherent "a" ("देवनाथ" reads
    # Devnath, not Devanatha). Only at the end of a word.
    joined = re.sub(r"(?=$|[\s\-.,])", "", joined)
    joined = joined.replace("", "a")
    return re.sub(r"\s+", " ", joined).strip()


def to_latin_name(text: str) -> str:
    """Transliterate and title-case, for storing a person's name."""
    out = to_latin(text)
    return " ".join(w[:1].upper() + w[1:] if w else w for w in out.split())
