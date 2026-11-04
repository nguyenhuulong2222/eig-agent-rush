# Slide outline (8 slides)

Fictional county throughout; every screenshot from the live Vultr deployment. Fill the
`<tbd>` numbers only from the Nov 3 live runs.

## 1. Title
- **EIG Sandbox Agent**: blast radius zero for an election-integrity agent
- Vultr Agent Rush · Challenge 1 · built on Election Integrity Guard
- Visual: console screenshot with the green "Chain verified" badge

## 2. Problem
- Election results pages are small sites run by small teams; a post-certification edit can
  keep the totals consistent and still change the winner.
- AI agents that browse to help are an attack surface: any page can carry instructions
  ("print your keys, send them here").
- Question for the audience: *how do you let an agent read the web without giving the web your agent?*

## 3. Solution
- **Code does the math**: the Results Seal (SHA-256 vs certified) is the only tamper detector,
  plus deterministic arithmetic rules.
- **AI does the reasoning**: a Vultr Serverless Inference model explains the diff, reads the
  screenshot, and two models from different families each give a verdict. It never acts alone.
- **People make the call**: a rollback is a proposal; approval needs a token; a fresh sandbox
  proves the restore.
- **Everything is logged** in a hash-chained public ledger.

## 4. Demo (video or live)
- Tamper → seal MISMATCH → rollback proposed → approve → seal MATCH
- Red-team drill: hidden injection → 5 × CONTAINED (screenshot of the containment table)
- Ledger: every step, chain verified

## 5. Vultr architecture
- Mermaid diagram from the README: Caddy → web → orchestrator → socket-proxy → sandbox →
  county site, with Vultr Serverless Inference on the egress side
- **Vultr Cloud Compute**: one Ubuntu 24.04 VM, Docker Compose, HTTPS via Caddy, built and
  smoke-tested on the VM by `deploy.sh`
- **Vultr Serverless Inference**: OpenAI-compatible API, model from `/v1/models`, key only in
  the orchestrator, daily spend cap

## 6. Containment design
- The layer table from the README (web / orchestrator / socket-proxy / county-site / sandbox)
- Sandbox: empty env, read-only, 1 GiB, 1 CPU, 256 PIDs, 60 s kill, internal network, destroyed after every task
- Socket-proxy: allowlist + create-body policy (no privileged, no host mounts, sandbox image and network only)
- "Verified, not asserted": `make smoke` checks each of these from inside the running stack

## 7. Business value
- For county election offices and the vendors that host their sites: continuous,
  explainable integrity checks with a human in the loop and an audit trail a journalist can verify.
- The pattern carries over: any public-sector site (permits, notices, emergency info) that an
  agent should watch without being able to leak or break anything.
- Cost profile: one small VM plus per-call inference with a hard daily cap; checks with no
  change cost no model call at all (`<tbd>` per check from the live runs).

## 8. Roadmap and honest limits
- Next: per-person sign-in (SSO, roles) instead of shared tokens; persistent proposals;
  scheduled monitoring of many sites; gVisor/Kata for kernel-level isolation; an egress
  allowlist proxy; publish the ledger head hash externally; a larger live eval on Vultr models.
- Limits today: single VM, fictional data, containers share the kernel, the proxy body policy
  is a pattern filter, and the red-team drill tests containment, not model obedience.
