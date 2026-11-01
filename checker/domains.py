"""Domain allowlist matching and lookalike detection."""

from __future__ import annotations

from typing import Iterable, Optional
from urllib.parse import urlsplit

# Characters commonly swapped in for each other in phishing domains.
# Keys are already lowercase, because hostnames are case-insensitive.
_CONFUSABLES = {
    "0": "o", "1": "l", "i": "l", "|": "l", "3": "e", "4": "a", "5": "s",
    "7": "t", "8": "b", "9": "g", "$": "s", "@": "a",
    # Cyrillic and Greek homoglyphs
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "l", "ӏ": "l", "ԁ": "d", "ѕ": "s", "ј": "j", "ο": "o", "ν": "v",
    "α": "a", "ε": "e", "κ": "k", "τ": "t",
}
_MULTI = (("rn", "m"), ("vv", "w"), ("cl", "d"))


def host_of(url: str) -> Optional[str]:
    """Lowercase host of an http(s) URL, IDNA-decoded. None for relative or non-web URLs."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return None
    return decode_idna(parts.hostname.lower().rstrip("."))


def decode_idna(host: str) -> str:
    labels = []
    for label in host.split("."):
        if label.startswith("xn--"):
            try:
                label = label.encode("ascii").decode("idna")
            except UnicodeError:
                pass
        labels.append(label)
    return ".".join(labels)


def is_allowed(host: str, allowed: Iterable[str]) -> bool:
    host = host.lower()
    for domain in allowed:
        domain = domain.lower()
        if host == domain or host.endswith("." + domain):
            return True
    return False


def skeleton(text: str) -> str:
    """Collapse visually confusable characters so lookalikes compare equal."""
    out = "".join(_CONFUSABLES.get(ch, ch) for ch in text.lower())
    for seq, repl in _MULTI:
        out = out.replace(seq, repl)
    return out.replace("-", "").replace("_", "")


def levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _core(domain: str) -> str:
    """The distinctive label of an allowed domain: 'kanaloacounty' for 'kanaloacounty.example'."""
    return domain.lower().split(".")[0]


def lookalike_of(host: str, allowed: Iterable[str], max_distance: int = 2) -> Optional[str]:
    """Return the allowed domain that `host` imitates, or None.

    A host that is itself allowed (or a subdomain of an allowed domain) is never a lookalike.
    """
    allowed = list(allowed)
    if is_allowed(host, allowed):
        return None
    host_skel = skeleton(host)
    labels = host.lower().split(".")
    for domain in allowed:
        core = _core(domain)
        core_skel = skeleton(core)
        # 1. The whole allowed domain hidden inside another host (kanaloacounty.example.evil.test).
        if domain.lower() in host:
            return domain
        # 2. Any label, or adjacent label pair, that collapses to the allowed core
        #    (kanal0acounty, kanaloa-county, kanaloa.county).
        candidates = labels + ["".join(labels[i:i + 2]) for i in range(len(labels) - 1)]
        for cand in candidates:
            cand_skel = skeleton(cand)
            if cand_skel == core_skel:
                return domain
            if len(core_skel) >= 6 and 0 < levenshtein(cand_skel, core_skel) <= max_distance:
                return domain
            # 3. The core appears with extra words around it (kanaloacounty-vote, votekanaloacounty).
            if len(core_skel) >= 6 and core_skel in cand_skel:
                return domain
        if core_skel and core_skel in host_skel and len(core_skel) >= 6:
            return domain
    return None
