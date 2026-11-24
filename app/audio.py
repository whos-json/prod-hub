"""Audio for stories: offline voiceover (Windows speech), decoding, mixing. No network, nothing to download."""
import asyncio
import io
import os
import subprocess
import tempfile
from pathlib import Path

import av
import numpy as np

SR = 44100
NO_WINDOW = 0x08000000
_SAY = r'''
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($env:SC_VOICE) { try { $s.SelectVoice($env:SC_VOICE) } catch {} }
$s.SetOutputToWaveFile($env:SC_OUT)
$s.Speak([IO.File]::ReadAllText($env:SC_TEXT))
$s.Dispose()
'''


def decode_audio(data: bytes, sr: int = SR) -> np.ndarray:
    """Any audio/video file -> float32 array (2, N) at `sr`."""
    chunks = []
    with av.open(io.BytesIO(data)) as c:
        rs = av.AudioResampler(format="fltp", layout="stereo", rate=sr)
        for fr in c.decode(audio=0):
            chunks += [r.to_ndarray() for r in rs.resample(fr)]
        chunks += [r.to_ndarray() for r in rs.resample(None)]
    return np.concatenate(chunks, axis=1) if chunks else np.zeros((2, 0), np.float32)


def _speak_sync(text: str, voice: str | None) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "t.txt").write_text(text, encoding="utf-8")  # the text travels in a file, never in a command line
        (d / "s.ps1").write_text(_SAY, encoding="utf-8")
        env = dict(os.environ, SC_TEXT=str(d / "t.txt"), SC_OUT=str(d / "o.wav"), SC_VOICE=voice or "")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(d / "s.ps1")],
                           env=env, capture_output=True, timeout=120, creationflags=NO_WINDOW)
        if r.returncode or not (d / "o.wav").exists():
            raise RuntimeError("voiceover failed: " + r.stderr.decode(errors="replace")[:200])
        return decode_audio((d / "o.wav").read_bytes())


async def speak(text: str, voice: str | None = None) -> np.ndarray:
    return await asyncio.get_running_loop().run_in_executor(None, _speak_sync, text, voice)


_STYLES = {  # (bpm, chord roots in semitones above A3, minor?, hats)
    "calm": (66, (0, 5, 3, 7), False, False),
    "warm": (88, (0, 7, 5, 3), False, False),
    "upbeat": (112, (0, 5, 7, 5), False, True),
    "moody": (76, (0, 8, 5, 7), True, False),
}


def synth_music(style: str, seconds: float) -> np.ndarray:
    """Simple synthesised background music (soft pads, plucked arpeggio, light hats) in the chosen mood; no model, no licence issues.
    shortcut: basic by design; swap in a generative model (ACE-Step) when quality matters more than zero downloads."""
    s = style.lower()
    key = "upbeat" if any(w in s for w in ("upbeat", "energ", "happy", "bright", "fun")) else \
          "moody" if any(w in s for w in ("moody", "dark", "dramatic", "sad")) else \
          "calm" if any(w in s for w in ("calm", "ambient", "soft", "relax", "chill")) else "warm"
    bpm, roots, minor, hats = _STYLES[key]
    n = int(seconds * SR) + SR
    t = np.arange(n, dtype=np.float32) / SR
    beat = 60.0 / bpm
    bar = beat * 4
    out = np.zeros((2, n), np.float32)
    rng = np.random.default_rng(7)
    freq = lambda semis: 220.0 * 2 ** (semis / 12)
    third = 3 if minor else 4
    for b in range(int(seconds / bar) + 2):
        root = roots[b % 4]
        t0 = int(b * bar * SR)
        if t0 >= n:
            break
        seg = min(int(bar * SR) + int(0.4 * SR), n - t0)
        tt = t[:seg]
        env = np.minimum(1, tt / 0.35) * np.minimum(1, (bar + 0.4 - tt) / 0.4)  # soft attack and release for the pad
        for semis in (root - 12, root, root + third, root + 7):
            for ch, detune in ((0, 0.997), (1, 1.003)):
                out[ch, t0:t0 + seg] += 0.07 * env * np.sin(2 * np.pi * freq(semis) * detune * tt) + 0.02 * env * np.sin(4 * np.pi * freq(semis) * tt)
        arp = (root + 12, root + 12 + third, root + 19, root + 12 + third)  # plucked eighth notes
        for k in range(8):
            ns = t0 + int(k * beat / 2 * SR)
            if ns >= n:
                break
            m = min(int(0.6 * SR), n - ns)
            tp = t[:m]
            pluck = 0.09 * np.exp(-tp * 5) * np.sin(2 * np.pi * freq(arp[k % 4] + 12) * tp)
            out[:, ns:ns + m] += pluck * np.array([[0.9], [1.1]] if k % 2 else [[1.1], [0.9]], np.float32)
        if hats:
            for k in range(8):
                ns = t0 + int((k + 0.5) * beat / 2 * SR)
                if ns + 2000 < n:
                    out[:, ns:ns + 2000] += (rng.standard_normal((2, 2000)) * 0.02 * np.exp(-np.arange(2000) / 350)).astype(np.float32)
    d = int(0.29 * SR)  # a little echo for space
    out[:, d:] += 0.25 * out[:, :-d]
    peak = float(np.abs(out).max()) or 1.0
    return (out / peak * 0.8)[:, : int(seconds * SR)]


def mix(seconds: float, voice: np.ndarray | None, music: np.ndarray | None, music_volume: float = 0.25) -> np.ndarray:
    """Voice on top of music. Music is looped or trimmed to the video, ducked under speech, and fades out at the end."""
    n = int(max(seconds, (voice.shape[1] / SR if voice is not None else 0)) * SR)
    out = np.zeros((2, n), np.float32)
    if music is not None and music.shape[1]:
        m = np.tile(music, (1, n // music.shape[1] + 1))[:, :n].copy()
        gain = np.full(n, music_volume if voice is None else music_volume * 0.6, np.float32)  # quieter while someone speaks
        fade = min(n, int(1.2 * SR))
        gain[-fade:] *= np.linspace(1, 0, fade, dtype=np.float32)
        m *= gain
        m[:, : int(0.3 * SR)] *= np.linspace(0, 1, int(0.3 * SR), dtype=np.float32)
        out += m
    if voice is not None:
        out[:, : voice.shape[1]] += voice[:, :n] * 0.95
    peak = float(np.abs(out).max())
    return out / peak * 0.9 if peak > 0.95 else out
