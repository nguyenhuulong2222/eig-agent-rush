# Held-out tamper-triage cases

Written and committed **before** any of them was run against a model. The triage prompt
was frozen at the same time; `tests/test_m1.py` fails if `triage.TRIAGE_SYSTEM` changes.

- Frozen prompt SHA-256: `588f117b4a2d2f8ecfe90b1944ab2387e7f7270570fa24fa7d4f8f85c1b564d7`
- Development cases (prompt was tuned while these were visible): `../cases/t01.json` … `t08.json`

| Case | Expected | What it probes |
|---|---|---|
| H01 | legitimate | (a) recount of one contest only |
| H02 | legitimate | (b) precinct whose ballot style has only one measure |
| H03 | tampering | (c) vote shift behind a plausible, two-person-approved ticket (ticket says ±9 votes, diff moves 800) |
| H04 | legitimate | control: ballots added consistently across every contest |
| H05 | tampering | (c) candidate names swapped under a "display order" ticket |
| H06 | legitimate | turnout-only change (registered voters reconciled) |
| H07 | uncertain | vague "data refresh" ticket, single approver |

H01 and H02 check that the "a ballot carries every contest" rule added after T03 does not
push legitimate single-contest changes to ROLLBACK.
