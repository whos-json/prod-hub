"""Smoke test against a RUNNING gateway (start.bat, or NO_BROWSER=1 python gateway.py).
   python test_gateway.py            image + video + auth + validation + admin
   python test_gateway.py --llm      also swaps to the text model and back
Uses small sizes so it finishes in a few minutes; it checks correctness, not speed."""
import json, sys, time, urllib.request, urllib.error
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent  # project root
BASE, ADMIN = "http://127.0.0.1:8000", "http://127.0.0.1:8001"
TOKEN = (HERE / "data" / "token.txt").read_text().strip()
fails = []


def call(method, url, body=None, token=TOKEN, headers=None, raw=False):
    h = dict(headers or {})
    if token:
        h["Authorization"] = f"Bearer {token}"
    if body is not None:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=900) as r:
            data = r.read()
            return r.status, data if raw else json.loads(data or b"{}")
    except urllib.error.HTTPError as e:
        data = e.read()
        try:
            return e.code, json.loads(data)
        except Exception:
            return e.code, data


def call_headers(url):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return dict(r.headers)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        fails.append(name)


def run_job(kind, body):
    t = time.time()
    st, r = call("POST", f"{BASE}/{kind}", body)
    check(f"{kind}: submit returns 202", st == 202, f"{st} {r}")
    if st != 202:
        return None
    jid = r["job_id"]
    while time.time() - t < 1500:
        st, j = call("GET", f"{BASE}/jobs/{jid}")
        if j["status"] in ("done", "error"):
            break
        time.sleep(3)
    check(f"{kind}: job done", j["status"] == "done", j)
    if j["status"] != "done":
        return None
    st, data = call("GET", f"{BASE}/jobs/{jid}/result", raw=True)
    magic = data[:4] if st == 200 else b""
    ok = magic == b"\x89PNG" if kind == "image" else data[4:8] == b"ftyp"
    check(f"{kind}: result is a valid {'PNG' if kind == 'image' else 'MP4'} ({len(data)} bytes, {time.time() - t:.0f}s)", ok)
    want = "image/png" if kind == "image" else "video/mp4"
    check(f"{kind}: content-type is {want}", call_headers(f"{BASE}/jobs/{jid}/result").get("Content-Type") == want)
    return jid


# --- no model needed ---
st, h = call("GET", f"{BASE}/health", token=None)
check("health open + fields", st == 200 and {"loaded", "busy", "queue_length", "accepting"} <= set(h), h)
check("no token -> 401", call("POST", f"{BASE}/image", {"prompt": "x"}, token=None)[0] == 401)
check("wrong token -> 401", call("POST", f"{BASE}/image", {"prompt": "x"}, token="nope")[0] == 401)
check("missing prompt -> 400", call("POST", f"{BASE}/image", {})[0] == 400)
check("bad width -> 400", call("POST", f"{BASE}/image", {"prompt": "x", "width": 100})[0] == 400)
check("too many pixels -> 400", call("POST", f"{BASE}/image", {"prompt": "x", "width": 1280, "height": 1280})[0] == 400)
check("non-multiple-of-16 -> 400", call("POST", f"{BASE}/image", {"prompt": "x", "width": 500})[0] == 400)
check("scenes: empty list -> 400", call("POST", f"{BASE}/video", {"scenes": []})[0] == 400)
check("scenes: 11 scenes -> 400", call("POST", f"{BASE}/video", {"scenes": [{"prompt": "x"}] * 11})[0] == 400)
check("scenes: scene without prompt -> 400", call("POST", f"{BASE}/video", {"scenes": [{"seconds": 1}]})[0] == 400)
check("scenes: 10 s scene -> 400", call("POST", f"{BASE}/video", {"scenes": [{"prompt": "x", "seconds": 10}]})[0] == 400)
check("scenes: over 20 s total -> 400", call("POST", f"{BASE}/video", {"scenes": [{"prompt": "x", "seconds": 4}] * 6})[0] == 400)
check("bad frames -> 400", call("POST", f"{BASE}/video", {"prompt": "x", "frames": 10})[0] == 400)
check("unknown job -> 404", call("GET", f"{BASE}/jobs/{'0' * 32}")[0] == 404)
check("path traversal id -> 404", call("GET", f"{BASE}/jobs/..%2f..%2ftoken.txt/result")[0] == 404)
st, _ = call("GET", f"{ADMIN}/api/state", token=None, headers={"Host": "evil.example"})
check("admin rejects foreign Host", st == 403, st)
st, _ = call("GET", f"{ADMIN}/api/state", token=None, headers={"Origin": "http://evil.example"})
check("admin rejects foreign Origin", st == 403, st)
st, a = call("GET", f"{ADMIN}/api/state", token=None)
check("admin state ok", st == 200 and a["token"] == TOKEN and "system" in a, st)
check("admin not reachable with token path on public port", call("GET", f"{BASE}/api/state")[0] == 404)

# --- hardening found in review ---
import http.client
import numpy as np
def raw(method, path, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", 8000, timeout=30)
    c.request(method, path, headers=headers or {})
    return c.getresponse().status
check("non-ASCII Authorization -> 401, not 500", raw("GET", "/v1/models", {"Authorization": "Bearer \xe9"}) == 401)
check("/v1/../ path trick -> 400", raw("GET", "/v1/../slots", {"Authorization": f"Bearer {TOKEN}"}) in (400, 404))
check("/v1/ encoded path trick -> 400", raw("GET", "/v1/%2e%2e/props", {"Authorization": f"Bearer {TOKEN}"}) in (400, 404))
check("seed 'abc' -> 400", call("POST", f"{BASE}/image", {"prompt": "x", "seed": "abc"})[0] == 400)
check("seed -1 -> 400", call("POST", f"{BASE}/image", {"prompt": "x", "seed": -1})[0] == 400)

# --- browser (CORS) ---
pre = urllib.request.Request(f"{BASE}/image", method="OPTIONS", headers={"Origin": "https://app.example", "Access-Control-Request-Method": "POST",
                                                                       "Access-Control-Request-Headers": "authorization,content-type"})
with urllib.request.urlopen(pre) as r:
    check("CORS preflight 204 with allow headers (no key needed)", r.status == 204 and r.headers["Access-Control-Allow-Origin"] == "https://app.example"
          and "authorization" in r.headers["Access-Control-Allow-Headers"].lower(), dict(r.headers))
req = urllib.request.Request(f"{BASE}/health", headers={"Origin": "https://app.example"})
with urllib.request.urlopen(req) as r:
    check("CORS header on normal response", r.headers["Access-Control-Allow-Origin"] == "https://app.example")
try:
    urllib.request.urlopen(urllib.request.Request(f"{BASE}/image", data=b"{}", method="POST", headers={"Origin": "https://app.example"}))
except urllib.error.HTTPError as e:
    check("CORS header also on 401 errors (browser can read the error)", e.code == 401 and e.headers.get("Access-Control-Allow-Origin") == "https://app.example")

# --- models ---
img = run_job("image", {"prompt": "a sunlit window with plants, no text", "width": 256, "height": 320, "steps": 2, "seed": 7})
# cancel: a queued job, a running job, and a finished job
SMALL = {"prompt": "a green plant, no text", "width": 256, "height": 256, "steps": 2}
st, h = call("GET", f"{BASE}/health", token=None)
check("health reports loading and first_job_slower", "loading" in h and "first_job_slower" in h and h["first_job_slower"] is False, h)
a_id = call("POST", f"{BASE}/image", SMALL)[1]["job_id"]
b_id = call("POST", f"{BASE}/image", SMALL)[1]["job_id"]
st, r = call("DELETE", f"{BASE}/jobs/{b_id}")
check("queued job can be cancelled (DELETE)", st == 200 and r.get("status") == "cancelled", (st, r))
check("cancelled job reports cancelled", call("GET", f"{BASE}/jobs/{b_id}")[1].get("status") == "cancelled")
check("cancelling a finished job -> 409", call("DELETE", f"{BASE}/jobs/{img}")[0] == 409)
check("cancelling an unknown job -> 404", call("DELETE", f"{BASE}/jobs/{'0' * 32}")[0] == 404)
while call("GET", f"{BASE}/jobs/{a_id}")[1]["status"] not in ("done", "error"):
    time.sleep(2)
long_id = call("POST", f"{BASE}/video", {"scenes": [{"prompt": "a calm cafe, no text", "seconds": 1}] * 4, "width": 256, "height": 256, "steps": 4})[1]["job_id"]
while call("GET", f"{BASE}/jobs/{long_id}")[1]["status"] != "running":
    time.sleep(1)
st, r = call("POST", f"{BASE}/jobs/{long_id}/cancel")
check("running job accepts cancel (POST /cancel)", st == 202, (st, r))
t0 = time.time()
while call("GET", f"{BASE}/jobs/{long_id}")[1]["status"] not in ("cancelled", "done", "error") and time.time() - t0 < 180:
    time.sleep(2)
check("running job ends as cancelled", call("GET", f"{BASE}/jobs/{long_id}")[1]["status"] == "cancelled")
vid = run_job("video", {"prompt": "slow pan across a sunlit cafe table, no text", "width": 256, "height": 256, "frames": 9, "steps": 4, "seed": 7})
story = run_job("video", {"scenes": [{"prompt": "a sunlit cafe table, no text", "seconds": 0.5}, {"prompt": "steam rising from a cup, no text", "seconds": 0.5},
                          {"prompt": "green plants by a window, no text", "seconds": 1}], "style": "warm morning light", "transition": "cut", "width": 256, "height": 256, "steps": 4, "seed": 3})
if story:
    st, j = call("GET", f"{BASE}/jobs/{story}")
    check("storyboard: progress 1.0 and 3 scenes reported", j.get("progress") == 1.0 and j.get("scenes_total") == 3 and j.get("scenes_done") == 3, j)
    st, data = call("GET", f"{BASE}/jobs/{story}/result", raw=True)
    import av, io
    with av.open(io.BytesIO(data)) as c:
        frames = sum(1 for _ in c.decode(video=0))
    check("storyboard: scenes joined into one clip (9+9+25 frames)", frames == 43, frames)
# preview: a quick contact sheet, one frame per scene
t0 = time.time()
pv = call("POST", f"{BASE}/video", {"scenes": [{"prompt": "a sunlit cafe table, no text"}, {"prompt": "a tidy shop shelf, no text"}, {"prompt": "green plants, no text"}], "preview": True, "seed": 2})[1]["job_id"]
while call("GET", f"{BASE}/jobs/{pv}")[1]["status"] not in ("done", "error"):
    time.sleep(2)
st, sheet = call("GET", f"{BASE}/jobs/{pv}/result", raw=True)
from PIL import Image as _Image
import io as _io
im = _Image.open(_io.BytesIO(sheet)) if st == 200 and sheet[1:4] == b"PNG" else None
check(f"preview is one PNG with a 256-wide tile per scene ({time.time() - t0:.0f}s for 3 scenes)", im is not None and im.size == (3 * 256 + 16, 448), im and im.size)

# photo animation + edits + sound, in one storyboard
import base64, av, io
from PIL import Image, ImageDraw
pic = Image.new("RGB", (400, 600), (235, 200, 120)); d = ImageDraw.Draw(pic)
d.rectangle([60, 300, 340, 520], fill=(120, 60, 30)); d.ellipse([140, 120, 260, 240], fill=(250, 250, 240))
buf = io.BytesIO(); pic.save(buf, "JPEG"); photo = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
check("scene with a non-image 'photo' -> 400", call("POST", f"{BASE}/video", {"scenes": [{"prompt": "x", "image": "bm90IGFuIGltYWdl"}]})[0] == 400)
check("unknown transition -> 400", call("POST", f"{BASE}/video", {"scenes": [{"prompt": "x"}], "transition": "spin"})[0] == 400)
check("voiceover too long -> 400", call("POST", f"{BASE}/video", {"prompt": "x", "voiceover": "a" * 700})[0] == 400)
edit = run_job("video", {"scenes": [{"prompt": "gentle camera push-in, soft light", "seconds": 1, "image": photo, "text": "Fresh every morning"},
                                    {"prompt": "steam rising from a cup, no text", "seconds": 1, "transition": "wipe"}],
                         "transition": "fade", "transition_ms": 300, "voiceover": "Fresh bread, every morning.", "music": "warm acoustic", "music_volume": 0.3,
                         "width": 256, "height": 384, "steps": 4, "seed": 5})
if edit:
    st, data = call("GET", f"{BASE}/jobs/{edit}/result", raw=True)
    with av.open(io.BytesIO(data)) as c:
        has_audio = bool(c.streams.audio) and c.streams.audio[0].codec_context.name == "aac"
        first = next(c.decode(video=0)).to_ndarray(format="rgb24")
    check("edited story has an AAC soundtrack", has_audio)
    ref = np.asarray(pic.resize((first.shape[1], first.shape[0]), Image.LANCZOS), dtype=float)
    check("photo scene starts from the owner's photo (first frame close to it)", abs(first.astype(float) - ref).mean() < 45, round(abs(first.astype(float) - ref).mean(), 1))
# still-first (a Flux picture per scene, then animated) and continuity (each scene starts from the last one's final frame)
two = [{"prompt": "a sunlit cafe table with a cup, no text"}, {"prompt": "steam rising from the cup, no text"}]
still_job = run_job("video", {"scenes": two, "stills": True, "width": 256, "height": 384, "steps": 4, "seed": 9})
if still_job:
    j = call("GET", f"{BASE}/jobs/{still_job}")[1]
    check("still-first: progress finished with 2 scenes reported", j.get("progress") == 1.0 and j.get("scenes_done") == 2, j)
chain_job = run_job("video", {"scenes": two, "continuity": True, "width": 256, "height": 256, "steps": 4, "seed": 9})
st, lib = call("GET", f"{ADMIN}/api/library", token=None)
ids = {i.get("job") for i in lib} if st == 200 else set()
check("library has both results with metadata", {img, vid} <= ids, ids)

if "--llm" in sys.argv:
    st, r = call("POST", f"{BASE}/v1/chat/completions", {"messages": [{"role": "user", "content": "Reply with the single word: pong"}], "max_tokens": 8})
    check("llm: chat completion", st == 200 and r.get("choices"), f"{st} {str(r)[:200]}")
    check("llm: swapped (comfy stopped)", call("GET", f"{BASE}/health", token=None)[1]["loaded"] == "llm")
    run_job("image", {"prompt": "a green plant, no text", "width": 256, "height": 256, "steps": 2})
    check("llm->comfy swap back", call("GET", f"{BASE}/health", token=None)[1]["loaded"] == "comfy")

print("\nALL PASSED" if not fails else f"\n{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
