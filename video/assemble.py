"""Assemble the demo video with FFmpeg from narration, the real screen recording, cards and Flow B-roll.

  .venv/bin/python assemble.py

Inputs: durations.json (tts.py), rec/marks.json + rec/session_*.webm (record.py),
build/cards + build/subs (cards.py), flow/01–03.mp4 (illustrative B-roll, opening/closing only).
Outputs: eig-agent-rush-demo.mp4, eig-agent-rush-demo.srt, contact-sheet.png, build/shots/*.png (deck).

This FFmpeg build has no libass/drawtext, so subtitles and labels are PNG overlays rendered by
cards.py; the .srt is exported separately.
"""
import json, subprocess
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
B = HERE / "build"
CLIPS = B / "clips"
FPS = 30
END_HOLD = 4.0          # end card after the last line of narration
ENC = ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-r", str(FPS),
       "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2"]

DUR = json.loads((HERE / "durations.json").read_text())
REC = json.loads((HERE / "rec" / "marks.json").read_text())
SUBS = json.loads((B / "subs.json").read_text())
LEAD, TAIL = REC["lead"], REC["tail"]
ORDER = [f"S{i:02d}" for i in range(1, 16)]
FLOW = {"S01": "flow/01.mp4", "S02": "flow/02.mp4", "S15": "flow/03.mp4"}
CARDS = {"S12", "S13", "S14"}


def sh(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def seg_len(sid):
    return LEAD + DUR[sid]["duration"] + TAIL + (END_HOLD if sid == "S15" else 0)


def fit(label_in, src_len, L, label_out):
    """Speed up (if longer) or hold the last frame (if shorter) so the stream lasts exactly L seconds."""
    if src_len > L:
        return f"[{label_in}]setpts=PTS*{L / src_len:.5f},fps={FPS},trim=0:{L:.3f},setpts=PTS-STARTPTS[{label_out}]"
    return (f"[{label_in}]fps={FPS},tpad=stop_mode=clone:stop_duration={L - src_len + 0.1:.3f},"
            f"trim=0:{L:.3f},setpts=PTS-STARTPTS[{label_out}]")


def base_clip(sid):
    """Video for one segment without subtitles, plus its narration track."""
    L = seg_len(sid)
    out = CLIPS / f"{sid}_base.mp4"
    audio = HERE / DUR[sid]["audio"]
    inputs, chain = [], []
    norm = "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1"
    if sid in FLOW:
        src = HERE / FLOW[sid]
        n = probe(src)
        inputs += ["-i", str(src), "-loop", "1", "-i", str(B / "cards/tag.png")]
        if sid == "S15":
            # Flow 03 under the narration, then the end card for the rest.
            inputs += ["-loop", "1", "-t", f"{L:.3f}", "-i", str(B / "cards/end.png")]
            chain += [f"[0:v]{norm},fps={FPS},setpts=PTS-STARTPTS[f]",
                      "[f][1:v]overlay=0:0:shortest=1,format=yuv420p[ft]",
                      f"[2:v]{norm},fps={FPS},trim=0:{L - n + 0.6:.3f},setpts=PTS-STARTPTS,format=yuv420p[e]",
                      "[ft][e]xfade=transition=fade:duration=0.6:offset=" + f"{n - 0.6:.3f}" + "[x]",
                      f"[x]trim=0:{L:.3f},fade=t=out:st={L - 0.8:.3f}:d=0.8[v]"]
        else:
            stretch = min(L / n, 1.15)
            chain += [f"[0:v]{norm},setpts=PTS*{stretch:.4f}[s]", fit("s", n * stretch, L, "f"),
                      "[f][1:v]overlay=0:0:shortest=1[ft]"]
            if sid == "S01":
                inputs += ["-loop", "1", "-i", str(B / "cards/title_overlay.png")]
                chain += [f"[2:v]format=rgba,fade=t=in:st=0.6:d=0.8:alpha=1[t]",
                          "[ft][t]overlay=0:0:shortest=1,fade=t=in:st=0:d=0.6[v]"]
            else:
                chain += ["[ft]null[v]"]
    elif sid in CARDS:
        inputs += ["-loop", "1", "-t", f"{L:.3f}", "-i", str(B / "cards" / f"{sid}.png")]
        chain += [f"[0:v]{norm},fps={FPS},fade=t=in:st=0:d=0.4[v]"]
    else:
        parts = REC["marks"][sid]["parts"]
        labels = []
        for i, part in enumerate(parts):
            src = HERE / "rec" / REC["videos"][part["page"]]
            inputs += ["-i", str(src)]
            chain.append(f"[{i}:v]trim={part['start']:.3f}:{part['end']:.3f},setpts=PTS-STARTPTS,{norm},fps={FPS}[p{i}]")
            labels.append(f"[p{i}]")
        total = sum(p["end"] - p["start"] for p in parts)
        if len(parts) > 1:
            chain.append("".join(labels) + f"concat=n={len(parts)}:v=1:a=0[cat]")
        else:
            chain.append("[p0]null[cat]")
        chain.append(fit("cat", total, L, "v"))
    ai = len([x for x in inputs if x == "-i"])
    inputs += ["-i", str(audio)]
    chain.append(f"[{ai}:a]aresample=48000,adelay={int(LEAD * 1000)}:all=1,apad,atrim=0:{L:.3f}[a]")
    sh(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(chain), "-map", "[v]", "-map", "[a]",
        *ENC, "-t", f"{L:.3f}", str(out)])
    return out, L


def sub_clip(sid, base, L):
    out = CLIPS / f"{sid}.mp4"
    cues = SUBS[sid]
    inputs, chain, last = ["-i", str(base)], [], "0:v"
    for i, c in enumerate(cues, start=1):
        inputs += ["-i", str(B / "subs" / c["png"])]
        a, b = LEAD + c["start"], LEAD + c["end"]
        nxt = f"o{i}"
        chain.append(f"[{last}][{i}:v]overlay=0:0:enable='between(t,{a:.3f},{b:.3f})'[{nxt}]")
        last = nxt
    sh(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(chain), "-map", f"[{last}]", "-map", "0:a",
        *ENC, "-t", f"{L:.3f}", str(out)])
    return out


def srt_time(t):
    ms = int(round(t * 1000)); h, ms = divmod(ms, 3600000); m, ms = divmod(ms, 60000); s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def frame(src, t, dst):
    sh(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.3f}", "-i", str(src), "-frames:v", "1", str(dst)])


def main():
    CLIPS.mkdir(parents=True, exist_ok=True)
    (B / "shots").mkdir(exist_ok=True)
    offsets, t, files, bases = {}, 0.0, [], {}
    for sid in ORDER:
        base, L = base_clip(sid)
        bases[sid] = (base, L)
        files.append(sub_clip(sid, base, L))
        offsets[sid] = t
        print(f"{sid}  {L:6.2f}s  starts {t:7.2f}s")
        t += L
    lst = B / "concat.txt"
    lst.write_text("".join(f"file '{f.resolve()}'\n" for f in files))
    final = HERE / "eig-agent-rush-demo.mp4"
    sh(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
        "-movflags", "+faststart", str(final)])

    # SRT from the same cue timing as the burned-in subtitles
    n, lines = 0, []
    for sid in ORDER:
        for c in SUBS[sid]:
            n += 1
            a, b = offsets[sid] + LEAD + c["start"], offsets[sid] + LEAD + c["end"]
            lines.append(f"{n}\n{srt_time(a)} --> {srt_time(b)}\n{c['text']}\n")
    (HERE / "eig-agent-rush-demo.srt").write_text("\n".join(lines))

    # Frames for the slide deck (from the clips without subtitles)
    picks = {"tamper_check.png": ("S06", 0.12), "verdicts.png": ("S06", 0.62), "county_altered.png": ("S07", 0.6),
             "restored.png": ("S09", 0.3), "redteam.png": ("S10", 0.72), "ledger.png": ("S11", 0.45)}
    for name, (sid, frac) in picks.items():
        base, L = bases[sid]
        frame(base, L * frac, B / "shots" / name)

    # 10-frame contact sheet
    dur = probe(final)
    tw, th = 576, 324
    sheet = Image.new("RGB", (tw * 5, (th + 34) * 2), "white")
    font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 22)
    for i in range(10):
        ts = dur * (i + 0.5) / 10
        fp = B / f"cs_{i}.png"
        frame(final, ts, fp)
        im = Image.open(fp).resize((tw, th))
        x, y = (i % 5) * tw, (i // 5) * (th + 34)
        sheet.paste(im, (x, y))
        sid = next(s for s in reversed(ORDER) if offsets[s] <= ts)
        ImageDraw.Draw(sheet).text((x + 8, y + th + 5), f"{int(ts // 60)}:{ts % 60:04.1f}  {sid}", fill="black", font=font)
    sheet.save(HERE / "contact-sheet.png")
    size = final.stat().st_size
    print(f"final {dur:.2f}s ({int(dur // 60)}:{dur % 60:05.2f}), {size / 1e6:.1f} MB -> {final.name}")


if __name__ == "__main__":
    main()
