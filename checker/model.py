"""Findings and verdicts."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Optional

BLOCK = "BLOCK"
WARN = "WARN"
INFO = "INFO"
PASS = "PASS"

_RANK = {INFO: 0, WARN: 1, BLOCK: 2}


@dataclass(frozen=True)
class Finding:
    rule_id: str
    severity: str
    category: str
    title: str
    detail: str
    file: Optional[str] = None
    line: Optional[int] = None
    suggestion: Optional[str] = None

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}


@dataclass
class Report:
    findings: list = field(default_factory=list)

    def extend(self, findings) -> None:
        self.findings.extend(findings)

    @property
    def verdict(self) -> str:
        worst = max((_RANK[f.severity] for f in self.findings), default=0)
        if worst == _RANK[BLOCK]:
            return BLOCK
        if worst == _RANK[WARN]:
            return WARN
        return PASS

    def count(self, severity: str) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    def sorted_findings(self) -> list:
        return sorted(
            self.findings,
            key=lambda f: (-_RANK[f.severity], f.rule_id, f.file or "", f.line or 0),
        )
