"""Offline checks for transitions, captions, fades and audio (no GPU, no gateway needed, a few seconds)."""
import asyncio, io, sys
from pathlib import Path

import av
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import audio, media_edit as me

W, H = 128, 224
def clip(color, n):
    return me.encode([np.full((H, W, 3), color, np.uint8)] * n)
A, B, C = clip((220, 40, 40), 24), clip((40, 200, 60), 24), clip((50, 60, 220), 24)

def probe(mp4):
    with av.open(io.BytesIO(mp4)) as c:
        v, a = c.streams.video[0], (c.streams.audio[0] if c.streams.audio else None)
        frames = [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]
    return frames, a

fails = []
def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if not ok else ""))
    if not ok: fails.append(name)

# transitions: lengths and what the middle looks like
cut = me.assemble([A, B], [None, None], [("cut", 0)] * 2)
f, a = probe(cut)
check("cut keeps every frame (24+24)", len(f) == 48 and a is None, len(f))
fade = me.assemble([A, B], [None, None], [("cut", 0), ("fade", 500)])
f, _ = probe(fade)
check("crossfade overlaps frames (shorter than a cut)", len(f) < 48, len(f))
mid = f[len(f) // 2 - 1].mean(axis=(0, 1))
check("crossfade middle is a blend of both clips (red and green both present)", mid[0] > 60 and mid[1] > 60, mid)
dip = me.assemble([A, B], [None, None], [("cut", 0), ("dip", 600)])
f, _ = probe(dip)
check("dip goes through near-black", min(x.mean() for x in f[12:36]) < 60)
wipe = me.assemble([A, B], [None, None], [("cut", 0), ("wipe", 500)])
f, _ = probe(wipe)
mid = f[len(f) // 2 - 1]
check("wipe shows both clips side by side", abs(float(mid[:, 2, 0].mean()) - float(mid[:, -3, 0].mean())) > 60)

# captions and fades
cap = me.assemble([A], ["Fresh bread daily"], [("cut", 0)])
f, _ = probe(cap)
check("caption is drawn (frame differs from plain clip)", np.abs(f[-1].astype(int) - np.array((220, 40, 40))).mean() > 1)
fo = me.assemble([A, B], [None, None], [("cut", 0)] * 2, fade_in_ms=300, fade_out_ms=300)
f, _ = probe(fo)
check("fade in and out start and end dark", f[0].mean() < f[len(f) // 2].mean() * 0.5 and f[-1].mean() < f[len(f) // 2].mean() * 0.5)

# audio: synthesised music (mood styles) and mixing
for style in ("calm", "upbeat", "moody", "warm acoustic"):
    m = audio.synth_music(style, 6)
    check(f"music '{style}' is 6 s stereo, audible, not clipped", m.shape == (2, 6 * audio.SR) and 0.05 < float(np.abs(m).max()) <= 1.0, (m.shape, float(np.abs(m).max())))
mx = audio.mix(5, None, audio.synth_music("calm", 5), 0.5)
check("mix returns stereo of the right length", mx.shape[0] == 2 and abs(mx.shape[1] - 5 * audio.SR) < 2)
withmusic = me.assemble([A, B], [None, None], [("cut", 0)] * 2, audio=lambda sec: audio.mix(sec, None, audio.synth_music("warm", sec), 0.3))
f, a = probe(withmusic)
check("mp4 carries an AAC audio stream", a is not None and a.codec_context.name == "aac")

# voiceover (Windows offline speech)
v = asyncio.run(audio.speak("Fresh bread, baked every morning."))
check("voiceover renders speech (1-6 s, audible)", v.shape[0] == 2 and 1 * audio.SR < v.shape[1] < 6 * audio.SR and float(np.abs(v).max()) > 0.05, v.shape)
long_v = np.tile(v, (1, 3))
held = me.assemble([A], [None], [("cut", 0)], audio=lambda sec: audio.mix(sec, long_v, None))
f, a = probe(held)
check("video is held so a long voiceover is not cut off", len(f) > 24, len(f))

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
