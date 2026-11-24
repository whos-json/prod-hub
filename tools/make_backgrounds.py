"""Makes the generic stock backgrounds through the running gateway: python tools/make_backgrounds.py
Writes data/stock/<scene>-<n>-45.jpg (1080x1350) and <scene>-<n>-916.jpg (1080x1920), JPEG q85, plus data/stock/prompts.json.
Generates at 896x1120 / 720x1280 (exact 4:5 and 9:16, ~1 MP) and scales up 1.205x / 1.5x."""
import io, json, sys, time, urllib.request
from pathlib import Path
from PIL import Image, ImageFilter

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "stock"
TOKEN = (ROOT / "data" / "token.txt").read_text().strip()
BASE = "http://127.0.0.1:8000"

LIGHT = "soft natural window light"
TAIL = ("photoreal photograph, shallow depth of field, calm composition with clear empty space in the lower third for text. "
        "No text, no letters, no logos, no brand names, no people, no hands, no watermark.")
SCENES = {
    "cafe": "a small cafe interior with warm wooden furniture and an empty table in the foreground",
    "pour": "a stream of coffee pouring from a steel pitcher into a clear glass on a cafe counter",
    "shelf": "a tidy shop shelf with simple everyday goods in plain unlabelled packaging",
    "workbench": "a clean wooden workbench with neatly arranged hand tools",
    "studio": "an empty bright studio with a rolled yoga mat and a water bottle on a wooden floor",
}
SHAPES = {"45": (896, 1120, 1080, 1350), "916": (720, 1280, 1080, 1920)}
JOBS = [(s, 1) for s in SCENES] + [("cafe", 2)]  # 5 scenes + a second cafe variant = 6 pictures x 2 shapes = 12


def call(method, path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def make(prompt, w, h, seed):
    while True:
        try:
            jid = json.loads(call("POST", "/image", {"prompt": prompt, "width": w, "height": h, "steps": 4, "seed": seed}))["job_id"]
            break
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
            time.sleep(30)  # queue full: wait
    while True:
        j = json.loads(call("GET", f"/jobs/{jid}"))
        if j["status"] in ("done", "error", "cancelled"):
            break
        time.sleep(3)
    if j["status"] != "done":
        raise RuntimeError(f"generation {j['status']}: {j.get('error')}")
    return Image.open(io.BytesIO(call("GET", f"/jobs/{jid}/result"))).convert("RGB")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    log = json.loads((OUT / "prompts.json").read_text()) if (OUT / "prompts.json").exists() else {}
    for scene, n in JOBS:
        prompt = f"{SCENES[scene]}, {LIGHT}, {TAIL}"
        for shape, (w, h, tw, th) in SHAPES.items():
            name = f"{scene}-{n}-{shape}.jpg"
            if (OUT / name).exists():  # keep what is already made
                continue
            t = time.time()
            img = make(prompt, w, h, 1000 * (list(SCENES).index(scene) + 1) + n)
            img = img.resize((tw, th), Image.LANCZOS).filter(ImageFilter.UnsharpMask(radius=1.2, percent=60, threshold=2))
            name = f"{scene}-{n}-{shape}.jpg"
            img.save(OUT / name, "JPEG", quality=85, optimize=True)
            log[name] = dict(prompt=prompt, generated=f"{w}x{h}", saved=f"{tw}x{th}", seed=1000 * (list(SCENES).index(scene) + 1) + n, steps=4, seconds=round(time.time() - t))
            print(f"{name}  {log[name]['seconds']}s", flush=True)
            (OUT / "prompts.json").write_text(json.dumps(log, indent=2))


if __name__ == "__main__":
    main()
