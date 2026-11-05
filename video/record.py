"""Record the real EIG console on the live Vultr VM with Playwright (Chromium, 1920x1080, recordVideo).

One continuous browser session per page (console + county mirror) so that app state carries
from shot to shot; every shot's start/end is written to rec/marks.json and assemble.py cuts
one clip per shot from it. Pacing comes from the measured narration (durations.json).

Secrets: EIG_DEMO_TOKEN is read from the environment, typed into a password field, never printed.

Usage:
  .venv/bin/python record.py --dry            # overview + county + ledger only, no tamper / model run
  .venv/bin/python record.py --reset          # full take; runs the one allowed reset command first
"""
import argparse, json, os, subprocess, sys, time, urllib.request
from pathlib import Path
from playwright.sync_api import sync_playwright

HERE = Path(__file__).parent
BASE = os.environ.get("EIG_DEMO_URL", "https://104-238-180-117.sslip.io")
REC = HERE / "rec"
RUNS_FILE = HERE / "live_runs.json"     # live model runs used for the recording (cap 3)
MAX_LIVE_RUNS = 3
LEAD, TAIL = 0.4, 0.8                    # silence around each narration clip
ZOOM = 1.3
RESET_CMD = ["ssh", "eig@104.238.180.117", "cd ~/eig-agent-rush && make reset"]

DUR = {k: v["duration"] for k, v in json.loads((HERE / "durations.json").read_text()).items()}


def shot_len(sid):
    return LEAD + DUR[sid] + TAIL


INIT_JS = """
(() => {
  const z = %s;
  const install = () => {
    document.body.style.zoom = z;
    const st = document.createElement('style');
    st.textContent = `
      html { scroll-behavior: auto !important; }
      ::-webkit-scrollbar { display: none; }
      .eig-hl { outline: 4px solid #f5b301 !important; outline-offset: 4px; border-radius: 6px;
                box-shadow: 0 0 0 10px rgba(245,179,1,.18) !important; transition: outline-color .3s; }
      #eig-cursor { position: fixed; left: 0; top: 0; width: 34px; height: 34px; z-index: 2147483647;
                    pointer-events: none; transform: translate(-3px,-2px); }
      #eig-cursor.down svg { transform: scale(.85); }`;
    document.head.appendChild(st);
    const c = document.createElement('div');
    c.id = 'eig-cursor';
    c.innerHTML = '<svg width="34" height="34" viewBox="0 0 24 24"><path d="M3 2l7 19 2.6-7.6L20 11z" fill="#111" stroke="#fff" stroke-width="1.6" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(c);
    // clientX/Y are in zoomed-layout px; the fixed cursor lives inside the zoomed root.
    addEventListener('mousemove', e => { c.style.left = (e.clientX) + 'px'; c.style.top = (e.clientY) + 'px'; }, true);
    addEventListener('mousedown', () => c.classList.add('down'), true);
    addEventListener('mouseup', () => c.classList.remove('down'), true);
  };
  if (document.readyState === 'loading') addEventListener('DOMContentLoaded', install); else install();
})();
""" % ZOOM


class Rec:
    def __init__(self, ctx):
        self.ctx, self.pages, self.t0, self.marks = ctx, {}, {}, {}
        self.mouse = {}

    def page(self, name):
        if name not in self.pages:
            p = self.ctx.new_page()
            self.t0[name] = time.monotonic()
            self.pages[name] = p
            self.mouse[name] = (960, 540)
        self.pages[name].bring_to_front()
        return self.pages[name]

    def now(self, name):
        return round(time.monotonic() - self.t0[name], 3)

    def mark(self, sid, page, which, **extra):
        m = self.marks.setdefault(sid, {"parts": []})
        if which == "start":
            m["parts"].append({"page": page, "start": self.now(page)})
        else:
            m["parts"][-1]["end"] = self.now(page)
        m.update(extra)

    # --- motion helpers ---------------------------------------------------------------------
    def move(self, name, loc, steps=30, pause=0.15):
        p = self.pages[name]
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        if not box:
            return
        x, y = box["x"] + min(box["width"] / 2, 220), box["y"] + box["height"] / 2
        p.mouse.move(x, y, steps=steps)
        self.mouse[name] = (x, y)
        time.sleep(pause)

    def hl(self, name, loc, on=True):
        try:
            loc.evaluate("(el, on) => el.classList.toggle('eig-hl', on)", on)
        except Exception:
            pass

    def focus(self, name, loc, hold, move=True):
        """Scroll an element to the middle of the screen, point at it, highlight it for `hold` seconds.
        Cosmetic: a missing element only costs the hold time, never the take."""
        t_end = time.monotonic() + hold
        try:
            self._focus(name, loc, hold, move)
        except Exception as exc:
            print(f"  (focus skipped: {str(exc).splitlines()[0][:120]})")
        until(t_end)

    def _focus(self, name, loc, hold, move=True):
        loc = loc.first if hasattr(loc, "first") else loc
        loc.wait_for(state="visible", timeout=3000)
        loc.evaluate("el => el.scrollIntoView({behavior: 'smooth', block: 'center'})")
        time.sleep(0.7)
        if move:
            self.move(name, loc)
        self.hl(name, loc, True)
        time.sleep(max(0.0, hold - 0.85))
        self.hl(name, loc, False)

    def scroll_to(self, name, y_frac_selector, dur=0.8):
        self.pages[name].locator(y_frac_selector).first.evaluate(
            "el => el.scrollIntoView({behavior: 'smooth', block: 'start'})")
        time.sleep(dur)


def until(t_end):
    time.sleep(max(0.0, t_end - time.monotonic()))


def status():
    with urllib.request.urlopen(BASE + "/api/status", timeout=15) as r:
        return json.load(r)


def wait_for_reset_window(min_left=13.5 * 60):
    """Start just after an auto-reset: clean proposals, and it cannot fire again during the take."""
    while True:
        s = status()
        left = s["auto_reset"]["next_at"] - s["server_time"]
        if left >= min_left:
            print(f"auto-reset in {left/60:.1f} min: ok to record")
            return s
        print(f"auto-reset in {left:.0f}s; waiting for it to pass…")
        time.sleep(left + 20)


def use_live_run():
    used = json.loads(RUNS_FILE.read_text()) if RUNS_FILE.exists() else []
    if len(used) >= MAX_LIVE_RUNS:
        sys.exit(f"live-run cap reached ({MAX_LIVE_RUNS}); refusing to start another integrity check")
    used.append(time.strftime("%Y-%m-%dT%H:%M:%S"))
    RUNS_FILE.write_text(json.dumps(used))
    print(f"live model run {len(used)}/{MAX_LIVE_RUNS}")


def wait_job_done(p, timeout=90):
    p.wait_for_function("""() => { const r = document.querySelector('#result');
        const e = document.querySelector('#run-error');
        return (r && r.children.length > 0) || (e && !e.hidden); }""", timeout=timeout * 1000)
    if p.locator("#run-error").is_visible():
        raise RuntimeError("run failed: " + p.locator("#run-error").inner_text())


def county_number_locator(p, value):
    """The vote count on the county page that shows `value` (rendered as "17,916 votes")."""
    try:
        loc = p.locator("span.votes", has_text=f"{int(str(value).replace(',', '')):,} votes")
    except ValueError:
        return None
    return loc.first if loc.count() else None


def run(dry=False):
    token = os.environ.get("EIG_DEMO_TOKEN", "")
    if not dry and not token:
        sys.exit("EIG_DEMO_TOKEN is not set")
    REC.mkdir(exist_ok=True)
    for f in REC.glob("*.webm"):
        f.unlink()
    spend_before = status()["spend"]["spent_usd"]

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080}, device_scale_factor=1,
                                  record_video_dir=str(REC), record_video_size={"width": 1920, "height": 1080},
                                  color_scheme="light", bypass_csp=True)
        ctx.add_init_script(INIT_JS)
        R = Rec(ctx)
        C = "console"
        p = R.page(C)
        p.goto(BASE + "/", wait_until="networkidle")
        p.wait_for_selector("#status-chips .chip >> text=/Vultr|qwen/i", timeout=20000)
        p.mouse.move(960, 600)
        time.sleep(1.5)

        failed = None
        try:
            # S03 — overview -------------------------------------------------------------------
            t = time.monotonic(); R.mark("S03", C, "start")
            R.move(C, p.locator("h1"), pause=0.3)
            R.focus(C, p.locator("#status-chips"), 0.30 * shot_len("S03"))
            R.focus(C, p.locator(".lede"), 0.25 * shot_len("S03"))
            for sel in (".control:not(.attacker):not(.redteam)", ".control.attacker", ".control.redteam"):
                R.move(C, p.locator(sel).first, steps=25, pause=0.1)
                R.hl(C, p.locator(sel).first); time.sleep(0.9); R.hl(C, p.locator(sel).first, False)
            until(t + shot_len("S03")); R.mark("S03", C, "end")

            if not dry:
                # S04 — simulate tamper ----------------------------------------------------------
                t = time.monotonic(); R.mark("S04", C, "start")
                R.move(C, p.locator("#tamper-mode"), pause=0.3)
                p.select_option("#tamper-mode", "one")
                R.hl(C, p.locator("#tamper-mode")); time.sleep(1.2); R.hl(C, p.locator("#tamper-mode"), False)
                R.move(C, p.locator("#btn-tamper"), pause=0.4)
                p.click("#btn-tamper")
                p.wait_for_function("document.querySelector('#tamper-msg').textContent.startsWith('Tampered')", timeout=20000)
                R.hl(C, p.locator("#tamper-msg"))
                until(t + shot_len("S04")); R.hl(C, p.locator("#tamper-msg"), False); R.mark("S04", C, "end")

                # S05 — run integrity check, live step log ------------------------------------
                use_live_run()
                t = time.monotonic(); R.mark("S05", C, "start")
                R.move(C, p.locator("#btn-check"), pause=0.4)
                p.click("#btn-check")
                time.sleep(1.2)
                R.move(C, p.locator("#steps"), steps=40)
                R.hl(C, p.locator("#steps"))
                wait_job_done(p)
                done = time.monotonic()
                R.hl(C, p.locator("#steps"), False)
                until(max(done + 0.6, t + shot_len("S05")))
                R.mark("S05", C, "end", job_seconds=round(done - t, 2))

                # S06 — result ----------------------------------------------------------------
                L = shot_len("S06"); t = time.monotonic(); R.mark("S06", C, "start")
                R.focus(C, p.locator("#result .verdict .box").first, 0.22 * L)
                R.focus(C, p.locator("#result figure.shot").first, 0.13 * L)
                note = p.locator("#result p.note", has_text="Evidence note")
                if note.count():
                    R.focus(C, note.first, 0.14 * L)
                diff = p.locator("#result table", has=p.locator("th", has_text="Certified")).first
                R.focus(C, diff, 0.10 * L)
                verdicts = p.locator("#result h3", has_text="Model verdicts")
                if verdicts.count():
                    R.focus(C, verdicts.first.locator("xpath=following-sibling::div[1]"), 0.24 * L)
                R.focus(C, p.locator("#result .verdict .box").nth(1), 0.08 * L)
                callout = p.locator("#result .callout")
                if callout.count():
                    R.focus(C, callout.first, 0.05 * L)
                try:
                    cells = [c.strip() for c in diff.locator("tbody tr").first.locator("td").all_inner_texts()]
                except Exception:
                    cells = ["", "", ""]
                live_val, cert_val = cells[2], cells[1]
                print("diff row:", cells)
                until(t + L); R.mark("S06", C, "end", altered_value=live_val, certified_value=cert_val,
                                     diff_field=cells[0])

                # S07 — county mirror shows the altered number -------------------------------
                K = "county"
                k = R.page(K)
                k.goto(BASE + "/county/results.html", wait_until="networkidle")
                time.sleep(1.0)
                t = time.monotonic(); R.mark("S07", K, "start")
                loc = county_number_locator(k, live_val.replace(",", ""))
                if loc:
                    R.focus(K, loc.locator("xpath=ancestor::tr[1]") if loc.locator("xpath=ancestor::tr[1]").count() else loc,
                            shot_len("S07") - 0.5)
                until(t + shot_len("S07")); R.mark("S07", K, "end", found_altered=bool(loc))

                # S08 — human decision: token + approve ----------------------------------------
                p = R.page(C)
                t = time.monotonic(); R.mark("S08", C, "start")
                R.focus(C, p.locator("#decide h2"), 1.2, move=False)
                R.move(C, p.locator("#token"), pause=0.3)
                p.click("#token")
                p.fill("#token", token)          # like a paste; masked password field, never printed
                card = p.locator("#proposals .card.pending").first
                R.focus(C, card, 0.30 * shot_len("S08"))
                R.move(C, card.locator("button", has_text="Approve restore"), pause=0.6)
                card.locator("button", has_text="Approve restore").click()
                until(t + shot_len("S08")); R.mark("S08", C, "end")

                # S09 — restored + verified, then county mirror with the certified number ------
                t = time.monotonic(); R.mark("S09", C, "start")
                wait_job_done(p)
                R.focus(C, p.locator("#result .verdict .box").nth(1), 0.25 * shot_len("S09"))
                p.wait_for_selector("#proposals .badge.good", timeout=20000)
                R.focus(C, p.locator("#proposals .card").first, 0.22 * shot_len("S09"))
                R.mark("S09", C, "end")
                k = R.page(K)
                k.reload(wait_until="networkidle"); time.sleep(0.5)
                R.mark("S09", K, "start")
                certified_val = R.marks["S06"].get("certified_value")
                loc = county_number_locator(k, certified_val.replace(',', '')) if certified_val else None
                if loc:
                    R.focus(K, loc.locator("xpath=ancestor::tr[1]") if loc.locator("xpath=ancestor::tr[1]").count() else loc,
                            max(1.0, t + shot_len("S09") - time.monotonic() - 0.2))
                until(t + shot_len("S09")); R.mark("S09", K, "end", found_certified=bool(loc))

            # S10 — red-team drill ------------------------------------------------------------
            if not dry:
                p = R.page(C)
                L = shot_len("S10"); t = time.monotonic(); R.mark("S10", C, "start")
                p.evaluate("window.scrollTo({top: 0, behavior: 'smooth'})"); time.sleep(0.8)
                R.move(C, p.locator("#btn-redteam"), pause=0.4)
                R.hl(C, p.locator(".control.redteam")); time.sleep(0.8); R.hl(C, p.locator(".control.redteam"), False)
                p.click("#btn-redteam")
                wait_job_done(p)
                R.focus(C, p.locator("#result blockquote.injection"), 0.24 * L)
                R.focus(C, p.locator("#result figure.shot").first, 0.08 * L)
                rows = p.locator("#result table.contain tbody tr")
                R.focus(C, p.locator("#result table.contain"), 0.06 * L)
                n = rows.count()
                per = (0.36 * L) / max(1, n)
                for i in range(n):
                    R.move(C, rows.nth(i), steps=15, pause=0.05)
                    R.hl(C, rows.nth(i)); time.sleep(per - 0.2); R.hl(C, rows.nth(i), False)
                R.focus(C, p.locator("#result .verdict .box").first, max(1.0, t + L - time.monotonic() - 0.2))
                until(t + L); R.mark("S10", C, "end", attempts=n)

            # S11 — ledger ------------------------------------------------------------------------
            p = R.page(C)
            L = shot_len("S11"); t = time.monotonic(); R.mark("S11", C, "start")
            p.wait_for_selector("#ledger-badge.good", timeout=20000)
            R.focus(C, p.locator("#ledger h2"), 0.12 * L, move=False)
            R.focus(C, p.locator("#ledger-badge"), 0.25 * L)
            rows = p.locator("#ledger-table tbody tr")
            for i in range(min(rows.count(), 8)):
                R.move(C, rows.nth(i), steps=12, pause=0.05)
                time.sleep(max(0.2, 0.40 * L / 8 - 0.3))
            R.focus(C, p.locator("#ledger-badge"), max(1.0, t + L - time.monotonic() - 0.2))
            until(t + L); R.mark("S11", C, "end", badge=p.locator("#ledger-badge").inner_text())
            time.sleep(1.0)

        except Exception as exc:  # keep whatever was recorded
            import traceback; traceback.print_exc(); failed = exc
        videos = {name: pg.video for name, pg in R.pages.items()}
        ctx.close()
        out = {}
        for name, v in videos.items():
            dst = REC / f"session_{name}.webm"
            v.save_as(str(dst)); out[name] = dst.name
        browser.close()

    spend_after = status()["spend"]["spent_usd"]
    (REC / "marks.json").write_text(json.dumps({"videos": out, "marks": R.marks, "lead": LEAD, "tail": TAIL,
                                                "spend_before": spend_before, "spend_after": spend_after,
                                                "dry": dry}, indent=2))
    print(json.dumps(R.marks, indent=1))
    print(f"status spend: {spend_before} -> {spend_after} USD (estimated at ceiling prices)")
    if failed:
        sys.exit(f"take incomplete: {failed}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--reset", action="store_true", help="run the one allowed reset command first")
    a = ap.parse_args()
    if not a.dry:
        wait_for_reset_window()
    if a.reset:
        subprocess.run(RESET_CMD, check=True)
    run(dry=a.dry)
