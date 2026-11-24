"""Storyboard editing: scene clips -> captions, transitions, fades, audio -> one H.264/AAC MP4. PyAV + numpy + Pillow only.

Frames are held in memory as uint8 RGB (a 12 s 512x896 story is ~400 MB), which is fine for one job at a time."""
import io
import tempfile
from pathlib import Path

import av
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FPS = 24
TRANSITIONS = ("cut", "fade", "dip", "wipe")
_FONTS = (r"C:\Windows\Fonts\segoeuib.ttf", r"C:\Windows\Fonts\arialbd.ttf", "DejaVuSans-Bold.ttf")


def decode(mp4: bytes):
    with av.open(io.BytesIO(mp4)) as c:
        return [f.to_ndarray(format="rgb24") for f in c.decode(video=0)]


def _font(size):
    for f in _FONTS:
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def caption_layer(text, w, h):
    """RGBA overlay: bold white text on a soft dark pill, in the lower third (clear of the platform UI at the very bottom)."""
    font = _font(max(18, int(w * 0.075)))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if probe.textlength(trial, font=font) <= w * 0.8 or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = word
    lines.append(cur)
    line_h = int(font.size * 1.25)
    box_w = int(max(probe.textlength(l, font=font) for l in lines)) + int(font.size * 1.2)
    box_h = line_h * len(lines) + int(font.size * 0.7)
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x0, y0 = (w - box_w) // 2, int(h * 0.68) - box_h // 2
    d.rounded_rectangle([x0, y0, x0 + box_w, y0 + box_h], radius=int(font.size * 0.6), fill=(15, 14, 26, 150))
    for i, l in enumerate(lines):
        d.text((w // 2, y0 + int(font.size * 0.35) + i * line_h), l, font=font, fill=(255, 255, 255, 255), anchor="ma")
    return np.asarray(layer, dtype=np.float32)


def add_caption(frames, text):
    h, w = frames[0].shape[:2]
    layer = caption_layer(text, w, h)
    rgb, alpha = layer[..., :3], layer[..., 3:] / 255.0
    for i, f in enumerate(frames):
        a = alpha * min(1.0, (i + 1) / 6)  # quick fade-in
        frames[i] = (f * (1 - a) + rgb * a).astype(np.uint8)


def _mix(a, b, t):
    return (a.astype(np.float32) * (1 - t) + b.astype(np.float32) * t).astype(np.uint8)


def join(out, cur, kind, ms):
    """Append `cur` to `out` with a transition: cut | fade (crossfade) | dip (through black) | wipe (left to right)."""
    n = max(2, round(ms * FPS / 1000))
    n = min(n, len(out) // 2, len(cur) // 2)
    if kind == "cut" or n < 2:
        out.extend(cur)
    elif kind == "fade":
        for k in range(n):
            out[-n + k] = _mix(out[-n + k], cur[k], (k + 1) / (n + 1))
        out.extend(cur[n:])
    elif kind == "dip":
        half = n // 2
        black = np.zeros_like(out[0])
        for k in range(half):
            out[-half + k] = _mix(out[-half + k], black, (k + 1) / half)
            cur[k] = _mix(black, cur[k], (k + 1) / half)
        out.extend(cur)
    elif kind == "wipe":
        w = out[0].shape[1]
        for k in range(n):
            x = int(w * (k + 1) / (n + 1))
            f = out[-n + k].copy()
            f[:, :x] = cur[k][:, :x]
            out[-n + k] = f
        out.extend(cur[n:])
    else:
        raise ValueError(f"unknown transition {kind}")


def fade_edges(frames, fade_in_ms, fade_out_ms):
    black = np.zeros_like(frames[0])
    n = min(round(fade_in_ms * FPS / 1000), len(frames) // 3)
    for k in range(n):
        frames[k] = _mix(black, frames[k], (k + 1) / (n + 1))
    n = min(round(fade_out_ms * FPS / 1000), len(frames) // 3)
    for k in range(n):
        frames[-1 - k] = _mix(black, frames[-1 - k], (k + 1) / (n + 1))


def assemble(clips, captions, transitions, fade_in_ms=0, fade_out_ms=0, audio=None, sr=44100, match=False):
    """clips: scene MP4 bytes; captions/transitions: one per clip (transition = into that clip, ignored for the first).
    audio: float32 array (2, N) at `sr`, or None. Returns MP4 bytes."""
    frames = []
    for i, mp4 in enumerate(clips):
        cur = decode(mp4)
        if match:
            if i == 0:
                ref = stats(cur)
            else:
                match_color(cur, ref)
        if captions[i]:
            add_caption(cur, captions[i])
        if i == 0:
            frames = cur
        else:
            kind, ms = transitions[i]
            join(frames, cur, kind, ms)
    if fade_in_ms or fade_out_ms:
        fade_edges(frames, fade_in_ms, fade_out_ms)
    if callable(audio):  # audio is built once the final length is known
        audio = audio(len(frames) / FPS)
    if audio is not None:
        need = int(audio.shape[1] / sr * FPS) + 1  # hold the last frame so a long voiceover is never cut off
        frames.extend([frames[-1]] * min(max(0, need - len(frames)), 4 * FPS))
    return encode(frames, audio, sr)


def encode(frames, audio=None, sr=44100):
    with tempfile.TemporaryDirectory() as d:  # a real file, so the muxer can move the index to the front (faststart: plays while downloading)
        path = Path(d) / "out.mp4"
        with av.open(str(path), "w", format="mp4", options={"movflags": "faststart"}) as oc:
            v = oc.add_stream("libx264", rate=FPS)
            v.height, v.width, v.pix_fmt = frames[0].shape[0], frames[0].shape[1], "yuv420p"
            v.options = {"crf": "20"}
            a, pos, total = None, 0, 0
            if audio is not None:
                a = oc.add_stream("aac", rate=sr, layout="stereo")
                total = audio.shape[1]

            def push_audio(limit_s):
                nonlocal pos
                while pos < total and pos / sr < limit_s:  # interleave audio with video so it streams
                    chunk = np.ascontiguousarray(audio[:, pos:pos + 1024], dtype=np.float32)
                    af = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
                    af.sample_rate, af.pts = sr, pos
                    for pkt in a.encode(af):
                        oc.mux(pkt)
                    pos += 1024

            for i, arr in enumerate(frames):
                if a is not None:
                    push_audio(i / FPS + 0.25)
                for pkt in v.encode(av.VideoFrame.from_ndarray(arr, format="rgb24")):
                    oc.mux(pkt)
            for pkt in v.encode():
                oc.mux(pkt)
            if a is not None:
                push_audio(float("inf"))
                for pkt in a.encode():
                    oc.mux(pkt)
        return path.read_bytes()


def contact_sheet(clips, gap=8):
    """One PNG with the last frame of every scene clip side by side (a quick look at what each scene will become)."""
    tiles = [Image.fromarray(decode(c)[-1]) for c in clips]
    w, h = tiles[0].size
    sheet = Image.new("RGB", (len(tiles) * w + gap * (len(tiles) - 1), h), (244, 242, 248))
    for i, tile in enumerate(tiles):
        sheet.paste(tile, (i * (w + gap), 0))
    buf = io.BytesIO()
    sheet.save(buf, "PNG")
    return buf.getvalue()


def stats(frames):
    """Per-channel mean and spread over a sample of frames: the 'look' that later scenes are matched to."""
    x = np.stack(frames[:: max(1, len(frames) // 12)]).astype(np.float32)
    return x.mean(axis=(0, 1, 2)), x.std(axis=(0, 1, 2)) + 1e-3


def match_color(frames, ref, strength=0.85):
    """Pull a clip's colour balance and contrast toward `ref`. Chained scenes drift (more saturated, more orange each time); this stops it."""
    m, sd = stats(frames)
    for i, f in enumerate(frames):
        g = (f.astype(np.float32) - m) / sd * ref[1] + ref[0]
        frames[i] = np.clip(f * (1 - strength) + g * strength, 0, 255).astype(np.uint8)


def last_frame_png(mp4, ref=None):
    """The final frame of a clip as PNG bytes: the starting picture for the next scene when scenes are chained."""
    frame = decode(mp4)[-1:]
    if ref is not None:
        match_color(frame, ref)
    buf = io.BytesIO()
    Image.fromarray(frame[0]).save(buf, "PNG")
    return buf.getvalue()
