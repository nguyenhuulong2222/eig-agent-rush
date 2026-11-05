"""Generate one narration clip per segment with OpenAI TTS; write durations.json.

Reads OPENAI_API_KEY from the environment only (never printed).
Usage: .venv/bin/python tts.py [S03 S06 ...]   (default: all segments, skips existing)
"""
import json, re, subprocess, sys
from pathlib import Path
from openai import OpenAI

HERE = Path(__file__).parent
MODEL = "gpt-4o-mini-tts-2025-12-15"
VOICE = "cedar"
INSTRUCTIONS = ("Calm, clear, trustworthy documentary narrator. Steady, unhurried pace of about "
                "140 words per minute. Neutral American English. No dramatic emphasis.")
# Pronunciation only; subtitles keep the written form.
SPOKEN = {
    "qwen3.8-flash-next": "Qwen three point eight Flash Next",
    "glm-5.3-flash": "G-L-M five point three Flash",
    "/app/.env": "slash app, slash dot env",
    "evil.example": "evil dot example",
}


def segments():
    t = (HERE / "narration.md").read_text()
    return [dict(id=s, title=n, target=int(g), caption=c)
            for s, n, g, c in re.findall(r"## (S\d+) — (.*?) · target (\d+) s\n\nCaption: (.*?)\n", t)]


def spoken(text):
    for a, b in SPOKEN.items():
        text = text.replace(a, b)
    return text


def duration(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(path)], capture_output=True, text=True, check=True)
    return float(out.stdout.strip())


def main():
    only = set(sys.argv[1:])
    out = HERE / "audio"; out.mkdir(exist_ok=True)
    client = OpenAI()
    meta = {}
    chars = 0
    for seg in segments():
        f = out / f"{seg['id']}.wav"
        if (not only and not f.exists()) or seg["id"] in only:
            text = spoken(seg["caption"]); chars += len(text)
            with client.audio.speech.with_streaming_response.create(
                    model=MODEL, voice=VOICE, input=text, instructions=INSTRUCTIONS,
                    response_format="wav") as r:
                r.stream_to_file(f)
        seg["audio"] = str(f.relative_to(HERE)); seg["duration"] = round(duration(f), 3)
        meta[seg["id"]] = seg
        print(f"{seg['id']}  {seg['duration']:6.2f}s  (target {seg['target']}s)")
    (HERE / "durations.json").write_text(json.dumps(meta, indent=2))
    print(f"total speech {sum(s['duration'] for s in meta.values()):.1f}s; chars sent this run: {chars}")


if __name__ == "__main__":
    main()
