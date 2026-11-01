# Held-out set 2 (H08–H14): gate v1 vs gate v2

Written and committed **before** any model run. It exists because gate v2 (two independent
Nemotron Super verdicts; ROLLBACK only if both say tampering at ≥ 0.8; any disagreement →
ESCALATE) was designed after looking at results on H01–H07, so H01–H07 can no longer
measure v2 fairly. The triage prompt is unchanged (SHA-256 `588f117b…`, see ../heldout/README.md).

| Case | Expected | What it probes |
|---|---|---|
| H08 | legitimate | recount of one measure only (single-contest legitimate recount) |
| H09 | tampering | "duplicate batch removed" ticket, approved, but only one candidate loses votes |
| H10 | uncertain | vague "sync with county system" ticket, single approver |
| H11 | legitimate | publication timestamp fix, metadata only |
| H12 | tampering | registered voters cut by a service account at 02:40 via API token |
| H13 | legitimate | late overseas and military ballots added consistently to every contest |
| H14 | tampering | Yes/No counts swapped under a "ballot order" ticket (outcome flips) |
