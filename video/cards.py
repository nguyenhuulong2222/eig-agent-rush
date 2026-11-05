"""Render the video's cards, subtitle overlays and the submission slide deck with Playwright.

  .venv/bin/python cards.py cards     # build/cards/*.png  (title, S12–S14 slides, end card, tag)
  .venv/bin/python cards.py subs      # build/subs/<seg>_<n>.png + build/subs.json (cue timing per segment)
  .venv/bin/python cards.py deck      # slides.pdf (needs build/shots/*.png frames from the recording)

Everything is local HTML rendered by Chromium; no external assets.
"""
import html, json, re, sys
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
BUILD = HERE / "build"
REPO_URL = "github.com/nguyenhuulong2222/eig-agent-rush"
LIVE_URL = "104-238-180-117.sslip.io"
CREDIT = "Built by Long Nguyen"
VIDEO_URL = "youtu.be/JUmf_W9aaGk"

CSS = """
:root { --bg:#f7f8f6; --card:#fff; --ink:#1f2a2e; --muted:#4a5a5e; --line:#c9d3d1; --brand:#0b6e69;
        --hazard:#f5c518; --ok:#17663a; --ok-bg:#e5f3ea; --bad:#a4262c; --bad-bg:#fbe9ea; --info:#e8f1f0; }
* { box-sizing:border-box; margin:0; }
html,body { width:1920px; height:1080px; }
body { font-family:"Avenir Next",Avenir,"Helvetica Neue",Arial,sans-serif; color:var(--ink); background:var(--bg); }
.slide { width:1920px; height:1080px; position:relative; overflow:hidden; padding:88px 120px; display:flex; flex-direction:column; }
.band { position:absolute; left:0; right:0; top:0; height:14px; background:var(--hazard); }
.kicker { font-size:26px; font-weight:600; color:var(--brand); letter-spacing:.08em; text-transform:uppercase; }
h1 { font-size:72px; line-height:1.08; letter-spacing:-.01em; margin-top:14px; }
h2 { font-size:56px; line-height:1.1; margin-top:10px; }
p.lead { font-size:34px; line-height:1.4; color:var(--muted); margin-top:24px; max-width:1500px; }
.cols { display:grid; gap:36px; margin-top:44px; }
.c3 { grid-template-columns:repeat(3,1fr); } .c2 { grid-template-columns:1fr 1fr; }
.box { background:var(--card); border:2px solid var(--line); border-radius:18px; padding:30px 34px; }
.box h3 { font-size:32px; margin-bottom:12px; }
.box p, .box li { font-size:27px; line-height:1.4; color:var(--muted); }
.box ul { padding-left:30px; } .box li { margin:6px 0; }
.box.brand { border-color:var(--brand); } .box.bad { border-color:var(--bad); background:var(--bad-bg); }
.box.ok { border-color:var(--ok); background:var(--ok-bg); }
.foot { position:absolute; left:120px; right:120px; bottom:48px; font-size:22px; color:var(--muted);
        display:flex; justify-content:space-between; }
.pill { display:inline-block; padding:6px 16px; border-radius:999px; background:var(--info); color:var(--brand);
        font-weight:600; font-size:24px; margin:4px 8px 4px 0; }
.mono { font-family:ui-monospace,Menlo,monospace; }
img.shot { width:100%; border-radius:12px; border:2px solid var(--line); display:block; }
figcaption { font-size:24px; color:var(--muted); margin-top:10px; }
"""

ARCH_SVG = """
<svg width="1680" height="720" viewBox="0 0 1680 720" xmlns="http://www.w3.org/2000/svg" font-family="Avenir Next, Avenir, Helvetica, Arial">
 <defs><marker id="a" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="8" markerHeight="8" orient="auto-start-reverse">
   <path d="M0,0 L10,5 L0,10 z" fill="#4a5a5e"/></marker></defs>
 <style>.b{fill:#fff;stroke:#c9d3d1;stroke-width:2.5}.t{font-size:27px;font-weight:700;fill:#1f2a2e}
  .s{font-size:21px;fill:#4a5a5e}.l{stroke:#4a5a5e;stroke-width:2.5;fill:none;marker-end:url(#a)}
  .ll{font-size:19px;fill:#4a5a5e}</style>
 <rect x="0" y="300" width="190" height="110" rx="14" class="b"/>
 <text x="95" y="348" text-anchor="middle" class="t">Judge /</text><text x="95" y="380" text-anchor="middle" class="t">official</text>
 <rect x="250" y="20" width="1180" height="690" rx="22" fill="#e8f1f0" stroke="#0b6e69" stroke-width="3"/>
 <text x="280" y="62" class="t" fill="#0b6e69">Vultr Cloud Compute VM · control plane · Docker Compose</text>
 <rect x="290" y="300" width="200" height="110" rx="14" class="b"/>
 <text x="390" y="345" text-anchor="middle" class="t">Caddy</text><text x="390" y="378" text-anchor="middle" class="s">HTTPS</text>
 <rect x="540" y="280" width="250" height="150" rx="14" class="b"/>
 <text x="665" y="325" text-anchor="middle" class="t">web (public)</text>
 <text x="665" y="358" text-anchor="middle" class="s">UI · /api forwarder</text>
 <text x="665" y="388" text-anchor="middle" class="s">no secrets · no Docker</text>
 <rect x="850" y="90" width="270" height="170" rx="14" class="b" style="stroke:#0b6e69"/>
 <text x="985" y="135" text-anchor="middle" class="t">orchestrator</text>
 <text x="985" y="168" text-anchor="middle" class="s">inference key · spend cap</text>
 <text x="985" y="198" text-anchor="middle" class="s">Results Seal · gate</text>
 <text x="985" y="228" text-anchor="middle" class="s">hash-chained ledger</text>
 <rect x="850" y="320" width="270" height="120" rx="14" class="b"/>
 <text x="985" y="365" text-anchor="middle" class="t">socket-proxy</text>
 <text x="985" y="398" text-anchor="middle" class="s">allowlist + spec policy</text>
 <rect x="850" y="520" width="270" height="160" rx="14" fill="#fbe9ea" stroke="#a4262c" stroke-width="2.5"/>
 <text x="985" y="565" text-anchor="middle" class="t">sandbox (per task)</text>
 <text x="985" y="598" text-anchor="middle" class="s">Chromium · empty env</text>
 <text x="985" y="628" text-anchor="middle" class="s">read-only · 1 GiB · 60 s</text>
 <text x="985" y="658" text-anchor="middle" class="s">no internet · removed</text>
 <rect x="1170" y="520" width="230" height="160" rx="14" class="b"/>
 <text x="1285" y="575" text-anchor="middle" class="t">county site</text>
 <text x="1285" y="608" text-anchor="middle" class="s">fictional</text>
 <text x="1285" y="638" text-anchor="middle" class="s">results read-only</text>
 <rect x="1470" y="90" width="210" height="170" rx="14" fill="#fff" stroke="#0b6e69" stroke-width="3"/>
 <text x="1575" y="140" text-anchor="middle" class="t">Vultr</text>
 <text x="1575" y="172" text-anchor="middle" class="t">Serverless</text>
 <text x="1575" y="204" text-anchor="middle" class="t">Inference</text>
 <text x="1575" y="236" text-anchor="middle" class="s">qwen3.8 · glm-5.3</text>
 <path d="M190,355 L285,355" class="l"/>
 <path d="M490,355 L535,355" class="l"/>
 <path d="M790,320 L845,200" class="l"/><text x="700" y="250" class="ll">internal only</text>
 <path d="M1120,175 L1465,175" class="l"/><text x="1200" y="160" class="ll">egress: model calls only</text>
 <path d="M985,260 L985,315" class="l"/>
 <path d="M985,440 L985,515" class="l"/><text x="1000" y="485" class="ll">create · kill · remove</text>
 <path d="M1120,600 L1165,600" class="l"/>
 <text x="1170" y="505" class="ll">internal net, no route out</text>
</svg>"""


def page_html(body, transparent=False):
    bg = "body{background:transparent!important}" if transparent else ""
    return f"<!doctype html><html><head><meta charset='utf-8'><style>{CSS}{bg}</style></head><body>{body}</body></html>"


def foot(left="Election Integrity Guard · Vultr Agent Rush", right="Kanaloa County is fictional"):
    return f"<div class='foot'><span>{left}</span><span>{right}</span></div>"


# --- slides (used both as video cards and in the deck) ----------------------------------------------
def slide_title():
    return f"""<div class='slide' style='justify-content:center'><div class='band'></div>
      <div class='kicker'>Vultr Agent Rush · Challenge 1: Blast Radius Zero</div>
      <h1 style='font-size:96px'>Election Integrity Guard</h1>
      <p class='lead' style='font-size:42px'>A sandboxed agent on Vultr that catches tampered election results,
      proves every restore, and stays contained when a page tries to hijack it.</p>
      {foot("Long Nguyen", LIVE_URL)}</div>"""


def slide_problem():
    return f"""<div class='slide'><div class='band'></div><div class='kicker'>The problem</div>
      <h2>A results page is trusted by everyone and guarded by very few</h2>
      <div class='cols c2'>
        <div class='box bad'><h3>Silent changes</h3><ul>
          <li>After certification, one number is edited; totals may still add up.</li>
          <li>Translated voter pages drift from the English.</li>
          <li>Voters, candidates and reporters see the wrong thing; a small IT team finds out late.</li></ul></div>
        <div class='box bad'><h3>The agent is an attack surface</h3><ul>
          <li>Any page an AI agent reads can carry instructions.</li>
          <li>An agent with secrets, a shell or network access turns one page into a breach.</li></ul></div>
      </div>{foot()}</div>"""


def slide_solution():
    return f"""<div class='slide'><div class='band'></div><div class='kicker'>The solution</div>
      <h2>Code does the math. AI explains. People make the call.</h2>
      <div class='cols c3'>
        <div class='box brand'><h3>Results Seal decides</h3><p>SHA-256 of the live results vs the certified copy,
          plus arithmetic rules. The only tamper detector.</p></div>
        <div class='box brand'><h3>AI explains</h3><p>Vultr Serverless Inference: a vision read of the sandbox screenshot
          and verdicts from two model families (qwen3.8-flash-next, glm-5.3-flash).</p></div>
        <div class='box brand'><h3>A person approves</h3><p>Rollback is a proposal. A token holder approves; the orchestrator
          restores; a fresh sandbox re-reads and the seal must match.</p></div>
      </div>
      <p class='lead'>Every web read happens in a throwaway sandbox. Every step lands in a hash-chained public ledger.</p>
      {foot()}</div>"""


def slide_arch():
    return f"""<div class='slide' style='padding-top:70px'><div class='band'></div><div class='kicker'>Architecture on Vultr</div>
      <div style='margin-top:30px'>{ARCH_SVG}</div>{foot()}</div>"""


def slide_containment():
    return f"""<div class='slide'><div class='band'></div><div class='kicker'>Containment design</div>
      <h2>Blast radius zero: each layer holds only what it needs</h2>
      <div class='cols c2'>
        <div class='box'><h3>Per-task sandbox</h3><ul>
          <li>uid 10001, read-only rootfs, cap_drop ALL, no-new-privileges</li>
          <li>empty env, no secrets, no host mounts</li><li>1 GiB, 1 CPU, 256 PIDs, 60 s kill, removed after every task</li>
          <li>internal network only: no route to the internet</li></ul></div>
        <div class='box'><h3>Control plane</h3><ul>
          <li>web: public, no secrets, can't reach Docker</li>
          <li>orchestrator: inference key + spend cap; Docker only via socket-proxy</li>
          <li>socket-proxy: allowlisted API, refuses any non-sandbox spec</li>
          <li>restore written by the orchestrator only, after approval</li></ul></div>
      </div>
      <div class='box ok' style='margin-top:30px'><p style='color:#17663a;font-size:28px'><b>Red-team drill:</b>
        a hidden prompt injection asks for /app/.env, the environment and an upload to evil.example. A sandbox obeys:
        5/5 attempts CONTAINED, each logged with the error that stopped it.</p></div>{foot()}</div>"""


def slide_business():
    return f"""<div class='slide'><div class='band'></div><div class='kicker'>Business value</div>
      <h2>Who pays, and why this one</h2>
      <div class='cols c3'>
        <div class='box brand'><h3>Customers</h3><ul><li>County election offices</li><li>State election IT</li>
          <li>Civic-tech vendors that host results sites</li></ul></div>
        <div class='box brand'><h3>Pricing idea</h3><ul><li>Flat monthly fee per county</li><li>Statewide tier</li>
          <li>Inference is cheap: 60 checks/day ≈ $4.2 at conservative ceiling prices</li></ul></div>
        <div class='box brand'><h3>Why it's different</h3><ul><li>A deterministic seal decides</li>
          <li>AI only explains</li><li>A person approves every restore</li></ul></div>
      </div>{foot()}</div>"""


def slide_limits():
    return f"""<div class='slide'><div class='band'></div><div class='kicker'>Honest limits · roadmap</div>
      <h2>What it is not yet, and what's next</h2>
      <div class='cols c2'>
        <div class='box'><h3>Limitations</h3><ul><li>Fictional data, one VM, no high availability</li>
          <li>Containers share the host kernel (runc, not a VM)</li>
          <li>One live run per scenario: smoke-level, not a benchmark</li>
          <li>Shared approver tokens, not per-person identities</li></ul></div>
        <div class='box brand'><h3>Roadmap</h3><ul><li>gVisor or Kata for kernel isolation</li>
          <li>Single sign-on and roles for approvers</li><li>Seal page wording, not only results.json</li>
          <li>Pilot with a real county office</li></ul></div>
      </div>{foot()}</div>"""


def slide_end(video=False):
    vid = f"<br><span style='font-weight:600'>Video: {VIDEO_URL}</span>" if video else ""
    return f"""<div class='slide' style='justify-content:center;background:#0b6e69;color:#fff'>
      <div class='band'></div>
      <h1 style='font-size:84px;color:#fff'>Election Integrity Guard</h1>
      <p class='lead' style='color:#e8f1f0;font-size:40px;margin-top:30px'>
        <span style='font-weight:600'>{REPO_URL}</span><br><span style='font-weight:600'>https://{LIVE_URL}</span>{vid}</p>
      <p class='lead' style='color:#e8f1f0;font-size:32px;margin-top:50px'>{CREDIT}</p></div>"""


def title_overlay():
    """Transparent lower-third shown over the opening Flow clip."""
    return """<div style='position:absolute;left:0;right:0;top:330px;text-align:center'>
      <div style='display:inline-block;background:rgba(11,110,105,.88);color:#fff;padding:38px 70px;border-radius:22px'>
        <div style='font-size:84px;font-weight:700;letter-spacing:-.01em'>Election Integrity Guard</div>
        <div style='font-size:44px;margin-top:10px;color:#e8f1f0'>sandboxed agent on Vultr</div></div></div>"""


def tag_overlay():
    return """<div style='position:absolute;right:36px;top:30px;background:rgba(0,0,0,.55);color:#fff;
      font-size:26px;font-weight:600;letter-spacing:.06em;padding:8px 18px;border-radius:10px'>illustrative</div>"""


# --- subtitles ------------------------------------------------------------------------------------
def split_cues(text, max_chars=78):
    """Split a caption into cues of at most ~2 lines, breaking at sentence/clause boundaries when possible."""
    parts = re.split(r"(?<=[.!?:])\s+", text)
    cues = []
    for part in parts:
        words, cur = part.split(), ""
        for w in words:
            if len(cur) + 1 + len(w) > max_chars and cur:
                cues.append(cur); cur = w
            else:
                cur = (cur + " " + w).strip()
        if cur:
            cues.append(cur)
    # merge very short cues into the previous one
    out = []
    for c in cues:
        if out and len(c) < 22 and len(out[-1]) + len(c) + 1 <= max_chars + 14:
            out[-1] += " " + c
        else:
            out.append(c)
    return out


def nobreak(s):
    """Keep identifiers like qwen3.8-flash-next on one line (non-breaking hyphens)."""
    return re.sub(r"\S*-\S*", lambda m: m.group(0).replace("-", "\u2011"), s)


def sub_overlay(text):
    return f"""<div style='position:absolute;left:0;right:0;bottom:70px;text-align:center'>
      <span style='display:inline-block;max-width:1500px;background:rgba(10,14,16,.78);color:#fff;
        font-size:44px;line-height:1.3;font-weight:600;padding:12px 30px;border-radius:12px'>{nobreak(html.escape(text))}</span></div>"""


def render(pages, out_dir, transparent=False):
    out_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        for name, body in pages.items():
            pg.set_content(page_html(body, transparent))
            pg.screenshot(path=str(out_dir / f"{name}.png"), omit_background=transparent)
        b.close()


def cmd_cards():
    render({"S12": slide_arch(), "S13": slide_business(), "S14": slide_limits(), "end": slide_end()}, BUILD / "cards")
    render({"title_overlay": title_overlay(), "tag": tag_overlay()}, BUILD / "cards", transparent=True)


def cmd_subs():
    meta = json.loads((HERE / "durations.json").read_text())
    pages, timing = {}, {}
    for sid, seg in meta.items():
        cues = split_cues(seg["caption"])
        total = sum(len(c) for c in cues)
        t, items = 0.0, []
        for i, c in enumerate(cues):
            d = seg["duration"] * len(c) / total
            items.append({"png": f"{sid}_{i}.png", "text": c, "start": round(t, 3), "end": round(t + d, 3)})
            pages[f"{sid}_{i}"] = sub_overlay(c)
            t += d
        timing[sid] = items
    render(pages, BUILD / "subs", transparent=True)
    (BUILD / "subs.json").write_text(json.dumps(timing, indent=2))
    print(f"{len(pages)} cues")


def data_uri(path):
    """Inline a frame as JPEG (set_content pages cannot load file:// images)."""
    import base64, io
    from PIL import Image
    buf = io.BytesIO()
    Image.open(path).convert("RGB").save(buf, "JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def shot_slide(title, imgs):
    figs = "".join(f"<figure><img class='shot' src='{data_uri(BUILD / 'shots' / f)}'><figcaption>{c}</figcaption></figure>"
                   for f, c in imgs)
    return f"""<div class='slide' style='padding-top:70px'><div class='band'></div><div class='kicker'>Live demo · real Vultr VM</div>
      <h2 style='font-size:46px'>{title}</h2><div class='cols c2' style='margin-top:26px;gap:30px'>{figs}</div>{foot()}</div>"""


def cmd_deck():
    slides = [slide_title(), slide_problem(), slide_solution(),
              shot_slide("Tamper caught by code, explained by AI", [("tamper_check.png", "Seal MISMATCH, rollback proposed"),
                                                                     ("verdicts.png", "Two model families, vision evidence")]),
              shot_slide("A person approves; the restore is proven", [("county_altered.png", "County page: altered number"),
                                                                       ("restored.png", "RESTORED VERIFIED: seal MATCH")]),
              shot_slide("The agent is attacked; nothing gets out", [("redteam.png", "Red-team drill: all attempts contained"),
                                                                       ("ledger.png", "Hash-chained ledger: chain verified")]),
              slide_arch(), slide_containment(), slide_business(), slide_limits(), slide_end(video=True)]
    doc = page_html("".join(f"<section style='page-break-after:always'>{s}</section>" for s in slides))
    doc = doc.replace("html,body { width:1920px; height:1080px; }", "@page { size:1920px 1080px; margin:0 }")
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        pg.set_content(doc, wait_until="load")
        pg.pdf(path=str(HERE / "slides.pdf"), width="1920px", height="1080px", print_background=True,
               margin={"top": "0", "right": "0", "bottom": "0", "left": "0"})
        b.close()
    print(f"slides.pdf: {len(slides)} slides")


if __name__ == "__main__":
    {"cards": cmd_cards, "subs": cmd_subs, "deck": cmd_deck}[sys.argv[1]]()
