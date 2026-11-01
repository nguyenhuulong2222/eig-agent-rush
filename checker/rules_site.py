"""Site rules: official links, scripts and assets, and the Content Security Policy."""

from __future__ import annotations

import re
from typing import Optional

from .domains import host_of, is_allowed, lookalike_of
from .htmlscan import HtmlDoc
from .model import BLOCK, WARN, Finding

CAT_LINK = "link-integrity"
CAT_ASSET = "script-asset-integrity"
CAT_CSP = "security-headers"

_EVENT_ATTR = re.compile(r"^on[a-z]+$")
_URL_ATTRS = ("href", "src", "action", "formaction", "data", "poster")

# ---------------------------------------------------------------- links


def check_links(doc: HtmlDoc, path: str, allow: dict) -> list:
    out = []
    official = allow.get("official_link_domains", [])
    max_dist = int(allow.get("lookalike_max_distance", 2))
    for tag in doc.tags:
        for attr in _URL_ATTRS:
            url = tag.attrs.get(attr)
            if not url:
                continue
            if url.strip().lower().startswith("javascript:"):
                out.append(Finding("ASSET-005", BLOCK, CAT_ASSET, "javascript: URL in the page",
                                   f"<{tag.name} {attr}=\"{url[:60]}\"> runs code when used.",
                                   path, tag.line, "Use a normal link, or move the behavior into app.js."))
                continue
            host = host_of(url)
            is_official = "data-official-link" in tag.attrs
            if host is None:
                if is_official:
                    out.append(Finding("LINK-003", BLOCK, CAT_LINK, "Official link is not an absolute HTTPS URL",
                                       f"{tag.attrs['data-official-link'] or 'official'} link points to \"{url}\".",
                                       path, tag.line))
                continue
            imitated = lookalike_of(host, official, max_dist)
            if imitated:
                out.append(Finding("LINK-002", BLOCK, CAT_LINK, "Lookalike of an official domain",
                                   f"{host} imitates {imitated}. Voters following this link would leave "
                                   f"the official site without noticing.", path, tag.line,
                                   f"Point the link back to a {imitated} address, or remove it."))
                continue
            if is_official:
                if not is_allowed(host, official):
                    out.append(Finding("LINK-001", BLOCK, CAT_LINK, "Official link goes to a non-official domain",
                                       f"The {tag.attrs['data-official-link'] or 'official'} link points to {host}, "
                                       f"which is not in the official allowlist ({', '.join(official)}).",
                                       path, tag.line,
                                       "Use an allowlisted domain. Adding a domain to the allowlist needs code-owner approval."))
                elif not url.strip().lower().startswith("https://"):
                    out.append(Finding("LINK-003", BLOCK, CAT_LINK, "Official link does not use HTTPS",
                                       f"\"{url}\" can be intercepted or rewritten in transit.", path, tag.line,
                                       "Change the link to https://."))
    return out

# ---------------------------------------------------------------- scripts and assets


def check_assets(doc: HtmlDoc, path: str, allow: dict) -> list:
    out = []
    script_ok = allow.get("script_sources", [])
    iframe_ok = allow.get("iframe_sources", [])
    for tag in doc.tags:
        for attr in tag.attrs:
            if _EVENT_ATTR.match(attr):
                out.append(Finding("ASSET-004", BLOCK, CAT_ASSET, "Inline event handler",
                                   f"<{tag.name} {attr}=…> runs inline JavaScript, which the CSP forbids "
                                   "and reviewers easily miss.", path, tag.line,
                                   "Attach the handler from app.js with addEventListener."))
        if tag.name == "script":
            src = tag.attrs.get("src")
            if src is None:
                if tag.text.strip():
                    out.append(Finding("ASSET-002", BLOCK, CAT_ASSET, "Inline <script> block",
                                       "Inline scripts bypass review of app.js and are blocked by the CSP.",
                                       path, tag.line, "Move the code into a same-origin .js file."))
                    out.extend(scan_js(tag.text, path, tag.line - 1))
                continue
            host = host_of(src)
            if host and not is_allowed(host, script_ok):
                out.append(Finding("ASSET-001", BLOCK, CAT_ASSET, "Third-party script not on the allowlist",
                                   f"<script src> loads code from {host}. Whoever controls that host controls "
                                   "this page, including the results it shows.", path, tag.line,
                                   "Remove the script, or self-host a reviewed copy."))
        elif tag.name in ("iframe", "frame", "embed", "object"):
            src = tag.attrs.get("src") or tag.attrs.get("data") or ""
            host = host_of(src)
            if host is None or not is_allowed(host, iframe_ok):
                out.append(Finding("ASSET-003", BLOCK, CAT_ASSET, f"<{tag.name}> embeds content not on the allowlist",
                                   f"Embedded source: {src or '(none)'}.", path, tag.line,
                                   "Remove the embed, or get the source allowlisted through code-owner review."))
        elif tag.name == "link" and "stylesheet" in (tag.attrs.get("rel") or "").lower():
            host = host_of(tag.attrs.get("href", ""))
            if host:
                out.append(Finding("ASSET-008", WARN, CAT_ASSET, "Third-party stylesheet",
                                   f"Stylesheet loaded from {host}. CSS can restyle or hide official content.",
                                   path, tag.line, "Self-host the stylesheet."))
    return out


_JS_RULES = (
    (re.compile(r"\beval\s*\("), "ASSET-006", "eval() call",
     "eval runs strings as code; the CSP blocks it, so it is either dead code or an attempt to weaken the CSP."),
    (re.compile(r"\bnew\s+Function\s*\("), "ASSET-006", "new Function() call",
     "new Function runs strings as code, like eval."),
    (re.compile(r"\bset(?:Timeout|Interval)\s*\(\s*['\"`]"), "ASSET-006", "String passed to setTimeout/setInterval",
     "A string argument is evaluated as code, like eval."),
    (re.compile(r"\.(?:innerHTML|outerHTML)\s*=|\binsertAdjacentHTML\s*\(|\bdocument\.write(?:ln)?\s*\("),
     "ASSET-007", "Unsafe HTML sink",
     "Writing HTML strings into the page can inject markup from data; use textContent instead."),
)


def _strip_js_comments(src: str) -> str:
    # Keep line structure so line numbers stay correct.
    src = re.sub(r"/\*.*?\*/", lambda m: "\n" * m.group(0).count("\n"), src, flags=re.S)
    return re.sub(r"(?<![:\\\"'])//[^\n]*", "", src)


def scan_js(source: str, path: str, line_offset: int = 0) -> list:
    out = []
    for lineno, line in enumerate(_strip_js_comments(source).splitlines(), 1):
        for pattern, rule, title, detail in _JS_RULES:
            if pattern.search(line):
                out.append(Finding(rule, WARN, CAT_ASSET, title, detail, path, lineno + line_offset))
    return out

# ---------------------------------------------------------------- CSP


def parse_csp(value: str) -> dict:
    directives = {}
    for part in value.split(";"):
        tokens = part.split()
        if tokens:
            name = tokens[0].lower()
            if name not in directives:  # browsers ignore repeated directives
                directives[name] = [t if t.startswith("'") else t.lower() for t in tokens[1:]]
    return directives


def page_csp(doc: HtmlDoc) -> Optional[dict]:
    meta = doc.meta_http_equiv("Content-Security-Policy")
    if meta is None:
        return None
    return parse_csp(meta.attrs.get("content", ""))


# Directives that do not fall back to default-src, so removing them widens the policy.
_NO_FALLBACK = ("base-uri", "form-action", "frame-ancestors", "upgrade-insecure-requests", "sandbox")


def check_csp(doc: HtmlDoc, path: str, policy: dict, allow: dict, base_doc: Optional[HtmlDoc]) -> list:
    out = []
    meta = doc.meta_http_equiv("Content-Security-Policy")
    if meta is None:
        return [Finding("CSP-001", BLOCK, CAT_CSP, "Content Security Policy is missing",
                        "This page has no <meta http-equiv=\"Content-Security-Policy\">. GitLab Pages cannot set "
                        "headers, so without it any injected script would run.", path, None,
                        "Copy the CSP meta tag from index.html.")]
    csp = parse_csp(meta.attrs.get("content", ""))
    forbidden = {s.lower() for s in policy.get("forbidden_source_expressions", [])}
    allowed_hosts = list(allow.get("script_sources", [])) + list(allow.get("iframe_sources", []))

    for name, sources in csp.items():
        for src in sources:
            if src.lower() in forbidden:
                out.append(Finding("CSP-003", BLOCK, CAT_CSP, f"CSP allows {src}",
                                   f"{name} includes {src}, which defeats the purpose of the policy.",
                                   path, meta.line, f"Remove {src} from {name}."))
            elif not src.startswith("'") and src not in ("self",) and not src.endswith(":"):
                host = host_of(src if "://" in src else "https://" + src.lstrip("*."))
                if host and not is_allowed(host, allowed_hosts):
                    out.append(Finding("CSP-002", BLOCK, CAT_CSP, f"CSP trusts a host that is not allowlisted",
                                       f"{name} allows {src}.", path, meta.line,
                                       "Remove the host, or allowlist it through code-owner review."))

    for name, required in policy.get("required_directives", {}).items():
        have = csp.get(name)
        if have is None:
            out.append(Finding("CSP-002", BLOCK, CAT_CSP, f"CSP directive {name} is missing",
                               f"Required: {name} {' '.join(required)}.", path, meta.line))
            continue
        missing = [s for s in required if s not in have]
        if missing:
            out.append(Finding("CSP-002", BLOCK, CAT_CSP, f"CSP directive {name} was changed",
                               f"Required: {name} {' '.join(required)}; found: {name} {' '.join(have) or '(empty)'}.",
                               path, meta.line))

    for name, value in policy.get("required_meta", {}).items():
        tag = doc.meta_name(name)
        if tag is None or (tag.attrs.get("content") or "").strip().lower() != value.lower():
            out.append(Finding("CSP-005", WARN, CAT_CSP, f"<meta name=\"{name}\"> is missing or changed",
                               f"Expected content=\"{value}\".", path, None))

    if base_doc is not None:
        base = page_csp(base_doc)
        if base is not None:
            for name, sources in csp.items():
                added = [s for s in sources if s not in base.get(name, [])]
                if name in base and added:
                    out.append(Finding("CSP-004", BLOCK, CAT_CSP, f"CSP weakened: {name} widened",
                                       f"This change adds {' '.join(added)} to {name}.", path, meta.line,
                                       "Revert the CSP change; widening the policy needs a security review."))
                elif name not in base and name not in ("upgrade-insecure-requests", "block-all-mixed-content") \
                        and "default-src" in base and sources != base.get("default-src"):
                    out.append(Finding("CSP-004", BLOCK, CAT_CSP, f"CSP weakened: new {name} directive",
                                       f"{name} {' '.join(sources)} overrides default-src "
                                       f"{' '.join(base['default-src'])}.", path, meta.line,
                                       "Revert the CSP change; widening the policy needs a security review."))
            for name in base:
                if name not in csp and (name in _NO_FALLBACK or name == "default-src"):
                    out.append(Finding("CSP-004", BLOCK, CAT_CSP, f"CSP weakened: {name} removed",
                                       f"The base version had {name} {' '.join(base[name])}.", path, meta.line))
    return out


def check_security_policy_change(head: dict, base: Optional[dict], path: str) -> list:
    """The policy file itself must not be loosened."""
    if not isinstance(base, dict):
        return []
    out = []
    for name, sources in base.get("required_directives", {}).items():
        now = head.get("required_directives", {}).get(name)
        if now is None or now != sources:
            out.append(Finding("CSP-006", BLOCK, CAT_CSP, f"Security policy loosened: {name}",
                               f"Required {name} changed from {sources} to {now}.", path, None,
                               "Policy changes need a separate, security-reviewed MR."))
    removed = set(base.get("forbidden_source_expressions", [])) - set(head.get("forbidden_source_expressions", []))
    if removed:
        out.append(Finding("CSP-006", BLOCK, CAT_CSP, "Security policy loosened: forbidden sources removed",
                           f"No longer forbidden: {', '.join(sorted(removed))}.", path, None))
    return out


def check_allowlist_change(head: dict, base: Optional[dict], path: str) -> list:
    if not isinstance(base, dict):
        return []
    out = []
    for key in ("official_link_domains", "script_sources", "iframe_sources"):
        added = sorted(set(head.get(key, [])) - set(base.get(key, [])))
        if added:
            out.append(Finding("ALLOW-001", WARN, CAT_LINK if key == "official_link_domains" else CAT_ASSET,
                               f"Allowlist widened: {key}",
                               f"Added: {', '.join(added)}. Everything on this list is trusted by the checker.",
                               path, None, "Confirm the new entry with the elections IT owner."))
        for new in added:
            imitated = lookalike_of(new, base.get(key, []), int(head.get("lookalike_max_distance", 2)))
            if imitated:
                out.append(Finding("LINK-002", BLOCK, CAT_LINK, "Lookalike domain added to the allowlist",
                                   f"{new} imitates {imitated}.", path, None))
    if int(head.get("lookalike_max_distance", 2)) < int(base.get("lookalike_max_distance", 2)):
        out.append(Finding("ALLOW-002", WARN, CAT_LINK, "Lookalike detection made less sensitive",
                           "lookalike_max_distance was lowered.", path, None))
    return out
