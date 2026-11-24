"""Local AI gateway + dashboard. Start with start.bat; closing that window stops everything.

PUBLIC app  (127.0.0.1:8000, the tunnel points here; bearer-token auth on everything except /health):
  POST /image  {"prompt","width","height","steps","seed"}            -> 202 {job_id}   Flux Schnell GGUF
  POST /video  one clip {"prompt","frames","image"?} or a storyboard {"scenes":[{"prompt","seconds","image"?,"text"?,"transition"?}],"style",
               "transition","transition_ms","fade_in_ms","fade_out_ms","voiceover","voice","music","music_volume"} -> 202 {job_id}   LTX-Video 2B
  GET  /jobs/{id}          status queued|running|done|error, queue_position, elapsed_s
  GET  /jobs/{id}/result   PNG / animated WEBP (read from the library, survives restarts)
  ANY  /v1/...             OpenAI-compatible, proxied to llama-server (Qwen2.5-VL 7B)
  GET  /health             no auth: loaded, busy, queue_length, accepting
ADMIN app   (127.0.0.1:8001 only, never tunnelled): dashboard, key management, library.

One backend runs at a time (the iGPU shares RAM with the OS). A request for the other
backend stops the current one first; idle backends stop after IDLE_S.
"""
import asyncio, base64, collections, ctypes, io, json, os, random, re, secrets, subprocess, threading, time, urllib.parse, urllib.request, uuid, webbrowser
from datetime import datetime
from pathlib import Path

import psutil
from aiohttp import web, ClientSession, ClientTimeout, FormData
from PIL import Image, ImageOps

import audio
import media_edit

HERE = Path(__file__).resolve().parent   # app/
ROOT = HERE.parent                        # project root; everything the gateway needs lives under it
ENGINE, MODELS, DATA, BIN = ROOT / "engine", ROOT / "models", ROOT / "data", ROOT / "bin"
COMFY = ENGINE / "comfyui"
LIB, LOGS, TOKEN_FILE = DATA / "library", DATA / "logs", DATA / "token.txt"
IDLE_S = int(os.environ.get("IDLE_S", 600))
PORT = int(os.environ.get("GATEWAY_PORT", 8000))
ADMIN_PORT = int(os.environ.get("ADMIN_PORT", 8001))
MAX_QUEUE = 8
MAX_SCENES = 10
MAX_FRAMES = 481  # 20 s at 24 fps
STILL_TAIL = "photoreal photograph, natural realistic colors, sharp focus, no text, no people"
MOTION_LEAD = "gentle smooth natural motion, slow steady camera. "
VIDEO_TAIL = ", natural realistic colors, sharp focus, smooth stable camera motion"
NO_WINDOW = 0x08000000

BACKENDS = {
    "comfy": dict(
        port=8190, ready="/system_stats", cwd=COMFY,
        cmd=[str(COMFY / ".venv/Scripts/python.exe"), "main.py", "--port", "8190", "--disable-auto-launch",
             "--extra-model-paths-config", str(ENGINE / "extra_model_paths.yaml")]),
    "llm": dict(
        port=8081, ready="/health", cwd=ENGINE / "llama-server",
        cmd=[str(ENGINE / "llama-server/llama-server.exe"),
             "-m", str(MODELS / "llm/Qwen2.5-VL-7B-Instruct-Q4_K_M.gguf"),
             "--mmproj", str(MODELS / "llm/mmproj-Qwen2.5-VL-7B-Instruct-Q8_0.gguf"),
             "--port", "8081", "-c", "8192"]),
}

lock = asyncio.Lock()
state = dict(ready=False, llm_wait=0, push="not configured", name=None, proc=None, last=time.time(), started=time.time(), tunnel_url=None, tunnel_proc=None, tunnel_log=collections.deque(maxlen=20))
JOBS = {}                                  # id -> job dict (status only; results live in LIB)
REQLOG = collections.deque(maxlen=300)     # public requests, newest last
queue = None


# ---------- token ----------
def write_token(t):
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(t)
    os.replace(tmp, TOKEN_FILE)  # atomic: a crash can't leave an empty key file


def load_token():
    DATA.mkdir(exist_ok=True)
    t = TOKEN_FILE.read_text().strip() if TOKEN_FILE.exists() else ""
    if len(t) < 32:  # missing, blank or truncated must never mean "no key needed"
        t = secrets.token_hex(24)
        write_token(t)
    return t


TOKEN = load_token()


# ---------- process management ----------
def kill_children_on_exit():
    """Windows job object: when this process dies for any reason (incl. closing the console), every child dies too."""
    k = ctypes.windll.kernel32

    class Basic(ctypes.Structure):
        _fields_ = [("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", ctypes.c_uint32), ("c", ctypes.c_size_t),
                    ("d", ctypes.c_size_t), ("e", ctypes.c_uint32), ("f", ctypes.c_size_t), ("g", ctypes.c_uint32), ("h", ctypes.c_uint32)]

    class IO(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in "abcdef"]

    class Ext(ctypes.Structure):
        _fields_ = [("Basic", Basic), ("Io", IO), ("a", ctypes.c_size_t), ("b", ctypes.c_size_t), ("c", ctypes.c_size_t), ("d", ctypes.c_size_t)]

    H = ctypes.c_void_p  # without explicit types ctypes truncates 64-bit handles
    k.CreateJobObjectW.restype, k.CreateJobObjectW.argtypes = H, [H, ctypes.c_wchar_p]
    k.GetCurrentProcess.restype = H
    k.SetInformationJobObject.argtypes = [H, ctypes.c_int, H, ctypes.c_uint32]
    k.AssignProcessToJobObject.argtypes = [H, H]
    job = k.CreateJobObjectW(None, None)
    e = Ext()
    e.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not (job and k.SetInformationJobObject(job, 9, ctypes.addressof(e), ctypes.sizeof(e)) and k.AssignProcessToJobObject(job, k.GetCurrentProcess())):
        print("WARNING: could not create job object; closing the window may leave servers running")
    return job


def url(name, path=""):
    return f"http://127.0.0.1:{BACKENDS[name]['port']}{path}"


def spawn(name):
    LOGS.mkdir(exist_ok=True)
    b = BACKENDS[name]
    log = open(LOGS / f"{name}.log", "ab")
    return subprocess.Popen(b["cmd"], cwd=b["cwd"], stdout=log, stderr=log, creationflags=NO_WINDOW)


async def stop():
    p = state["proc"]
    if p and p.poll() is None:
        try:  # the venv python.exe is a launcher; the real server is its child, so take down the whole tree
            kids = psutil.Process(p.pid).children(recursive=True)
        except psutil.Error:
            kids = []
        for proc in [p, *kids]:
            try:
                proc.terminate()
            except (psutil.Error, OSError):
                pass
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: p.wait(30))
        except subprocess.TimeoutExpired:
            p.kill()
        alive = await loop.run_in_executor(None, lambda: psutil.wait_procs(kids, timeout=10)[1])
        for k in alive:
            try:
                k.kill()
            except (psutil.Error, OSError):
                pass
    state.update(name=None, proc=None, ready=False)


async def ensure(name):
    """Caller must hold `lock`."""
    state["last"] = time.time()
    if state["name"] == name and state["ready"] and state["proc"].poll() is None:
        return
    await stop()
    state["proc"] = spawn(name)
    state["name"], state["ready"] = name, False
    async with ClientSession() as s:
        for _ in range(300):  # 5 min: first model load is slow on this machine
            if state["proc"].poll() is not None:
                state.update(name=None, proc=None, ready=False)
                raise web.HTTPBadGateway(text=f"{name} backend exited during startup (see logs/{name}.log)")
            try:
                async with s.get(url(name, BACKENDS[name]["ready"])) as r:
                    if r.status == 200:
                        state["ready"] = True
                        return
            except Exception:
                pass
            await asyncio.sleep(1)
    await stop()
    raise web.HTTPGatewayTimeout(text=f"{name} backend did not become ready")


async def reaper():
    while True:
        await asyncio.sleep(30)
        try:
            if state["name"] and not lock.locked() and time.time() - state["last"] > IDLE_S:
                async with lock:
                    if time.time() - state["last"] > IDLE_S:
                        await stop()
        except Exception as e:  # keep reaping; one bad stop must not end it silently
            print("reaper:", e)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # never follow a redirect with the key attached
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def endpoint_ok(endpoint):
    u = urllib.parse.urlsplit(endpoint)
    return u.username is None and bool(u.hostname) and (u.scheme == "https" or (u.scheme == "http" and u.hostname == "127.0.0.1"))


def push_url(tunnel_url, once=False):
    """Tell every hosted app our address. data/hosted_app.txt lists their endpoints, one per line (https, or http://127.0.0.1 for tests).
    Each gets POST {"url": ...} with the gateway key as bearer and checks it against its GATEWAY_TOKEN. Retries every 30 s until all
    answer, then re-sends every 5 min (idempotent) so an app that lost the address on a restart gets it back."""
    f = DATA / "hosted_app.txt"
    while True:
        lines = [l.strip() for l in f.read_text().splitlines() if l.strip() and not l.strip().startswith("#")] if f.exists() else []
        if not lines:
            state["push"] = "not configured (data/hosted_app.txt)"
            wait = 60
        else:
            results, all_ok = [], True
            for endpoint in lines:
                host = urllib.parse.urlsplit(endpoint).hostname or "?"
                if not endpoint_ok(endpoint):
                    results.append(f"{host}: refused, https only")  # never send the key over plain http
                    all_ok = False
                    continue
                try:
                    req = urllib.request.Request(endpoint, data=json.dumps({"url": tunnel_url}).encode(), method="POST",
                                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {TOKEN}"})
                    with _opener.open(req, timeout=15) as r:
                        results.append(f"{host}: updated {datetime.now():%H:%M}")
                except Exception as e:
                    results.append(f"{host}: not reached ({str(e)[:50]})")
                    all_ok = False
            state["push"] = "; ".join(results)
            wait = 300 if all_ok else 30
        if once or state["tunnel_url"] != tunnel_url:  # a newer address has its own thread
            return
        time.sleep(wait)


def start_tunnel():
    """Optional: bin/cloudflared.exe. data/tunnel_token.txt => named tunnel (stable URL, put it in data/public_url.txt); else quick tunnel."""
    exe = BIN / "cloudflared.exe"
    if not exe.exists():
        return
    env, tok = dict(os.environ), DATA / "tunnel_token.txt"
    if tok.exists():
        env["TUNNEL_TOKEN"] = tok.read_text().strip()
        cmd = [str(exe), "tunnel", "--no-autoupdate", "run"]
        pub = DATA / "public_url.txt"
        state["tunnel_url"] = pub.read_text().strip() if pub.exists() else None
    else:
        cmd = [str(exe), "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"]
    p = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, creationflags=NO_WINDOW)
    state["tunnel_proc"] = p

    def pump():
        for line in p.stderr:
            state["tunnel_log"].append(line.strip()[-200:])
            m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", line)
            if m and m.group(0) != state["tunnel_url"]:
                state["tunnel_url"] = m.group(0)
                threading.Thread(target=push_url, args=(m.group(0),), daemon=True).start()
    threading.Thread(target=pump, daemon=True).start()


# ---------- ComfyUI workflows (API format) ----------
def flux_workflow(prompt, w, h, steps, seed):
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": "flux1-schnell-Q4_K_S.gguf"}},
        "2": {"class_type": "DualCLIPLoader", "inputs": {"clip_name1": "t5xxl_fp8_e4m3fn.safetensors", "clip_name2": "clip_l.safetensors", "type": "flux"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": "ae.safetensors"}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "5": {"class_type": "EmptySD3LatentImage", "inputs": {"width": w, "height": h, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "seed": seed, "steps": steps, "cfg": 1.0, "sampler_name": "euler",
                                                    "scheduler": "simple", "positive": ["4", 0], "negative": ["4", 0], "latent_image": ["5", 0], "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": "gw"}},
    }


def ltx_workflow(prompt, w, h, frames, steps, seed, image=None):
    """Text-to-video, or with `image` (a file already uploaded to ComfyUI) image-to-video: the photo is the first frame, then it moves."""
    wf = {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "ltxv-2b-0.9.8-distilled-fp8.safetensors"}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": "t5xxl_fp8_e4m3fn.safetensors", "type": "ltxv"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["2", 0]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": "worst quality, blurry, distorted", "clip": ["2", 0]}},
        "5": {"class_type": "LTXVConditioning", "inputs": {"positive": ["3", 0], "negative": ["4", 0], "frame_rate": 24.0}},
        "6": {"class_type": "EmptyLTXVLatentVideo", "inputs": {"width": w, "height": h, "length": frames, "batch_size": 1}},
        "7": {"class_type": "LTXVScheduler", "inputs": {"steps": steps, "max_shift": 2.05, "base_shift": 0.95, "stretch": True, "terminal": 0.1, "latent": ["6", 0]}},
        "8": {"class_type": "KSamplerSelect", "inputs": {"sampler_name": "euler"}},
        "9": {"class_type": "SamplerCustom", "inputs": {"model": ["1", 0], "add_noise": True, "noise_seed": seed, "cfg": 1.0, "positive": ["5", 0],
                                                         "negative": ["5", 1], "sampler": ["8", 0], "sigmas": ["7", 0], "latent_image": ["6", 0]}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["1", 2]}},
        "11": {"class_type": "CreateVideo", "inputs": {"images": ["10", 0], "fps": 24.0}},
        "12": {"class_type": "SaveVideo", "inputs": {"video": ["11", 0], "filename_prefix": "gw", "format": "mp4"}},
    }
    if image:
        wf["L"] = {"class_type": "LoadImage", "inputs": {"image": image}}
        wf["6"] = {"class_type": "LTXVImgToVideo", "inputs": {"positive": ["5", 0], "negative": ["5", 1], "vae": ["1", 2], "image": ["L", 0],
                                                              "width": w, "height": h, "length": frames, "batch_size": 1, "strength": 1.0}}
        wf["7"]["inputs"]["latent"] = ["6", 2]
        wf["9"]["inputs"].update(positive=["6", 0], negative=["6", 1], latent_image=["6", 2])
    return wf


async def drop_file(path):
    """Best-effort delete of a temporary file. Windows may still hold it for a moment after ComfyUI writes it; that must never fail a job."""
    for _ in range(10):
        try:
            path.unlink(missing_ok=True)
            return
        except OSError:
            await asyncio.sleep(0.5)
    print("could not remove temporary file:", path)


async def run_comfy(wf, timeout=3600):
    async with ClientSession(timeout=ClientTimeout(total=timeout)) as s:
        async with s.post(url("comfy", "/prompt"), json={"prompt": wf}) as r:
            body = await r.json()
            if r.status != 200:
                raise web.HTTPBadRequest(text=json.dumps(body))
        pid = body["prompt_id"]
        deadline = time.time() + timeout
        while time.time() < deadline:
            async with s.get(url("comfy", f"/history/{pid}")) as r:
                h = (await r.json()).get(pid)
            if h and h["status"].get("completed") is not None:
                if h["status"]["status_str"] != "success":
                    raise web.HTTPInternalServerError(text=json.dumps(h["status"]["messages"][-3:]))
                img = next(i for o in h["outputs"].values() for i in o.get("images", []))
                async with s.get(url("comfy", "/view"), params=dict(filename=img["filename"], subfolder=img["subfolder"], type=img["type"])) as r:
                    data = await r.read()
                if img["type"] == "output":  # the library is the only copy we keep
                    await drop_file(COMFY / "output" / img["subfolder"] / img["filename"])
                return data, img["filename"]
            await asyncio.sleep(2)
    raise web.HTTPGatewayTimeout(text="generation timed out")


async def upload_image(data, name):
    async with ClientSession(timeout=ClientTimeout(total=60)) as s:
        form = FormData()
        form.add_field("image", data, filename=name, content_type="image/png")
        form.add_field("overwrite", "true")
        async with s.post(url("comfy", "/upload/image"), data=form) as r:
            if r.status != 200:
                raise RuntimeError(f"photo upload failed ({r.status})")
            return (await r.json())["name"]


def clean_image(v):
    """A photo (base64 or data URL) -> re-encoded PNG: orientation applied, metadata stripped, longest side <= 1536."""
    try:
        raw = base64.b64decode(v.split(",", 1)[1] if v.startswith("data:") else v, validate=True)
    except Exception:
        raise web.HTTPBadRequest(text="image must be base64 (a data URL is fine)")
    if len(raw) > 8_000_000:
        raise web.HTTPBadRequest(text="image too large (max 8 MB)")
    try:
        im = Image.open(io.BytesIO(raw))
        if im.format not in ("JPEG", "PNG", "WEBP") or im.size[0] * im.size[1] > 40_000_000:
            raise ValueError
        im = ImageOps.exif_transpose(im).convert("RGB")
        im.thumbnail((1536, 1536))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        raise web.HTTPBadRequest(text="image must be a JPEG, PNG or WEBP photo")


async def finish(parts, post):
    """Captions, transitions, fades and sound over the scene clips -> one MP4."""
    voice = await audio.speak(post["voiceover"], post["voice"]) if post["voiceover"] else None
    audio_fn = None
    if voice is not None or post["music"]:
        audio_fn = lambda sec: audio.mix(sec, voice, audio.synth_music(post["music"], sec) if post["music"] else None, post["music_volume"])
    return await asyncio.get_running_loop().run_in_executor(
        None, lambda: media_edit.assemble(parts, post["captions"], post["transitions"], post["fade_in_ms"], post["fade_out_ms"], audio_fn, match=post["match"]))


# ---------- jobs + library ----------
def lib_path(kind, jid, when=None):
    when = when or datetime.now()
    ext = "png" if kind == "image" else "mp4"  # kind decides the folder and the format
    d = LIB / kind / when.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{when.strftime('%H%M%S')}_{jid}.{ext}"


def find_result(jid):
    if not re.fullmatch(r"[0-9a-f]{32}", jid):
        return None
    return next((p for p in LIB.glob(f"*/*/*_{jid}.*") if p.suffix != ".json"), None)


def submit(kind, params, build, source, scenes=1, images=None, post=None, sheet=False, chain=False, stills=None):
    if sum(j["status"] in ("queued", "running") for j in JOBS.values()) >= MAX_QUEUE:
        raise web.HTTPTooManyRequests(text=json.dumps({"error": "busy", "retry_after": 60}), content_type="application/json")
    jid = uuid.uuid4().hex
    JOBS[jid] = dict(id=jid, kind=kind, status="queued", created=time.time(), started=None, finished=None, error=None,
                     build=build, params=params, source=source, done=0, clips=0, scenes=scenes, stills=stills, total=scenes * (2 if stills else 1) + (1 if post else 0),
                     images=images or [None] * scenes, post=post, sheet=sheet, chain=chain, lib_kind="image" if sheet else kind)
    queue.put_nowait(jid)
    return jid


class Cancelled(Exception):
    pass


async def worker():
    while True:
        jid = await queue.get()
        j = JOBS[jid]
        if j["status"] == "cancelled":  # left the queue before it started
            continue
        j.update(status="running", started=time.time())
        names = []
        try:
            async with lock:
                await ensure("comfy")
                names = [await upload_image(img, f"gw_{jid}_{k}.png") if img else None for k, img in enumerate(j["images"])]
                loop = asyncio.get_running_loop()
                if j["stills"]:  # still-first: Flux makes a picture per scene (it draws far better than the video model), then each is animated
                    fw, fh = j["stills"]["size"]
                    for k in range(j["scenes"]):
                        if j.get("cancel"):
                            raise Cancelled()
                        if names[k] is None:
                            png, _ = await run_comfy(flux_workflow(j["stills"]["prompts"][k], fw, fh, 4, j["stills"]["seed"] + k))
                            names[k] = await upload_image(png, f"gw_{jid}_s{k}.png")
                        j["done"] += 1
                parts, ref = [], None
                for k in range(j["scenes"]):  # one clip per scene; the model stays loaded between them
                    if j.get("cancel"):
                        raise Cancelled()
                    nm = names[k]
                    if nm is None and j["chain"] and k > 0:  # continuity: this scene starts from where the last one ended (colour-matched to scene 1)
                        last = await loop.run_in_executor(None, media_edit.last_frame_png, parts[-1], ref)
                        nm = await upload_image(last, f"gw_{jid}_c{k}.png")
                        names.append(nm)
                    data, _ = await run_comfy(j["build"](k, nm))
                    parts.append(data)
                    if j["chain"] and k == 0:
                        ref = await loop.run_in_executor(None, lambda d=data: media_edit.stats(media_edit.decode(d)))
                    j["clips"] += 1
                    j["done"] += 1
                    state["last"] = time.time()
            if j["sheet"]:
                data = await asyncio.get_running_loop().run_in_executor(None, media_edit.contact_sheet, parts)
            elif j["post"]:
                data = await finish(parts, j["post"])
                j["done"] += 1
            if j.get("cancel"):
                raise Cancelled()
            p = lib_path(j["lib_kind"], jid)
            tmp = LIB / f".{jid}.part"  # outside the */*/ glob, so a half-written file is never served
            tmp.write_bytes(data)
            os.replace(tmp, p)
            p.with_suffix(".json").write_text(json.dumps(
                dict(job=jid, kind=j["lib_kind"], created=datetime.now().isoformat(timespec="seconds"), seconds=round(time.time() - j["started"], 1),
                     source=j["source"], **j["params"])))
            j["status"] = "done"
        except web.HTTPException as e:
            j.update(status="error", error=e.text or e.reason)
        except Exception as e:
            j.update(status="error", error=str(e)[:300])
        if j.get("cancel") and j["status"] != "done":  # whatever stopped it, the owner asked for this
            j.update(status="cancelled", error=None)
        for nm in names:  # the uploaded photos are not kept
            if nm:
                await drop_file(COMFY / "input" / nm)
        j["images"] = j["build"] = None
        j["finished"] = time.time()
        for k in [k for k, v in JOBS.items() if v["finished"] and time.time() - v["finished"] > 3600]:
            del JOBS[k]


def _int(d, k, default, lo, hi, multiple=1):
    try:
        v = int(d.get(k, default))
    except (TypeError, ValueError, OverflowError):
        raise web.HTTPBadRequest(text=f"{k} must be an integer")
    if not lo <= v <= hi or v % multiple:
        raise web.HTTPBadRequest(text=f"{k} must be {lo}..{hi}, multiple of {multiple}")
    return v


def _text(d, k, maxlen):
    v = d.get(k)
    v = "" if v is None else str(v).strip()
    if len(v) > maxlen:
        raise web.HTTPBadRequest(text=f"{k} too long (max {maxlen} chars)")
    return v


def make_job(kind, d, source):
    prompt = str(d.get("prompt", "")).strip()
    if kind == "video" and "scenes" in d:
        prompt = "scenes"  # each scene carries its own prompt
    if not prompt or len(prompt) > 2000:
        raise web.HTTPBadRequest(text="prompt required (max 2000 chars)")
    try:
        seed = int(d["seed"]) if "seed" in d else random.randrange(2**32)
    except (TypeError, ValueError, OverflowError):
        raise web.HTTPBadRequest(text="seed must be an integer")
    if not 0 <= seed < 2**32:
        raise web.HTTPBadRequest(text="seed must be 0..4294967295")
    if kind == "image":
        w, h = _int(d, "width", 768, 256, 1280, 16), _int(d, "height", 960, 256, 1280, 16)
        if w * h > 1_100_000:  # keeps one job within the iGPU's shared-memory budget
            raise web.HTTPBadRequest(text="width*height must be <= 1,100,000 pixels (e.g. 1024x1024 max)")
        steps = _int(d, "steps", 4, 1, 12)
        return submit(kind, dict(prompt=prompt, width=w, height=h, steps=steps, seed=seed), lambda k, nm: flux_workflow(prompt, w, h, steps, seed), source)

    # ---- video: one clip, or a storyboard of scenes; any scene may start from a photo ----
    w, h = _int(d, "width", 512, 256, 1024, 32), _int(d, "height", 896, 256, 1024, 32)  # 9:16 by default; the app upscales
    if w * h > 600_000:  # 576x1024 max; bigger risks running out of shared memory
        raise web.HTTPBadRequest(text="width*height must be <= 600,000 pixels (576x1024 max)")
    steps = _int(d, "steps", 8, 1, 20)
    style = _text(d, "style", 300)
    multi = "scenes" in d
    if multi:
        raw = d["scenes"]
        if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_SCENES or not all(isinstance(x, dict) for x in raw):
            raise web.HTTPBadRequest(text=f"scenes must be a list of 1..{MAX_SCENES} objects")
    else:
        frames = _int(d, "frames", 121, 9, 241)  # 5 s at 24 fps by default (~90 s on this iGPU)
        if (frames - 1) % 8:
            raise web.HTTPBadRequest(text="frames must be 8n+1 (9, 17, 25, ...)")
        raw = [dict(prompt=prompt, frames=frames, image=d.get("image"), text=d.get("text"))]
    stills = bool(d.get("stills")) and not bool(d.get("preview"))  # each scene starts from a Flux picture of it, then moves
    chain = multi and bool(d.get("continuity"))  # each text-only scene continues from the previous scene's last frame (process videos)
    preview = bool(d.get("preview"))
    if preview:  # quick look before the real render: 256-wide, 9 frames per scene, 4 steps; the result is ONE PNG with a frame per scene
        w, h, steps = 256, max(256, round(256 * h / w / 32) * 32), 4
    default_t = _text(d, "transition", 10) or "fade"
    if default_t not in media_edit.TRANSITIONS:
        raise web.HTTPBadRequest(text=f"transition must be one of {', '.join(media_edit.TRANSITIONS)}")
    t_ms = _int(d, "transition_ms", 400, 100, 1500)
    prompts, frames_list, images, captions, transitions = [], [], [], [], []
    for sc in raw:
        text = _text(sc, "prompt", 500)
        if not text:
            raise web.HTTPBadRequest(text="each scene needs a prompt (what moves or happens)")
        if preview:
            fr = 9
        elif "frames" in sc:
            fr = sc["frames"]
        else:
            try:
                secs = float(sc.get("seconds", 2))
            except (TypeError, ValueError):
                raise web.HTTPBadRequest(text="seconds must be a number")
            if not 0.5 <= secs <= 4:
                raise web.HTTPBadRequest(text="scene seconds must be 0.5..4")
            fr = max(9, min(97, 8 * round((round(secs * 24) - 1) / 8) + 1))  # nearest valid 8n+1 length
        frames_list.append(fr)
        prompts.append(f"{style}, {text}" if style else text)  # the model follows long, concrete descriptions best
        images.append(clean_image(sc["image"]) if sc.get("image") else None)
        captions.append(_text(sc, "text", 80) or None)
        t = _text(sc, "transition", 10) or default_t
        if t not in media_edit.TRANSITIONS:
            raise web.HTTPBadRequest(text=f"transition must be one of {', '.join(media_edit.TRANSITIONS)}")
        transitions.append((t, t_ms))
    if multi and not preview and sum(frames_list) > MAX_FRAMES:
        raise web.HTTPBadRequest(text="total length must be <= 20 seconds")
    fade_in, fade_out = _int(d, "fade_in_ms", 0, 0, 1500), _int(d, "fade_out_ms", 400 if multi else 0, 0, 1500)
    voiceover, voice, music = _text(d, "voiceover", 600), _text(d, "voice", 60), _text(d, "music", 200)
    try:
        music_volume = float(d.get("music_volume", 0.25))
    except (TypeError, ValueError):
        raise web.HTTPBadRequest(text="music_volume must be a number 0..1")
    if not 0 <= music_volume <= 1:
        raise web.HTTPBadRequest(text="music_volume must be a number 0..1")
    post = None
    if multi or voiceover or music or any(captions) or fade_in or fade_out:
        post = dict(captions=captions, transitions=transitions, fade_in_ms=fade_in, fade_out_ms=fade_out,
                    voiceover=voiceover, voice=voice or None, music=music, music_volume=music_volume, match=chain)
    still_info = None
    if stills:
        s_ = (590_000 / (w * h)) ** 0.5  # Flux draws at about 0.6 MP, the video model then fits it to w x h
        still_info = dict(prompts=[f"{pr}, {STILL_TAIL}" for pr in prompts], size=(round(w * s_ / 16) * 16, round(h * s_ / 16) * 16), seed=seed)
    vprompts = [f"{MOTION_LEAD}{pr}" if stills else f"{pr}{VIDEO_TAIL}" for pr in prompts]
    params = dict(prompt=" | ".join(x["prompt"] for x in raw)[:300] if multi else prompt, width=w, height=h, frames=sum(frames_list), steps=steps, seed=seed)
    if multi:
        params.update(scenes=[str(x["prompt"]) for x in raw], style=style)
        if chain:
            params["continuity"] = True
    if stills:
        params["stills"] = True
    params.update({k: v for k, v in dict(photos=sum(i is not None for i in images), voiceover=voiceover, music=music).items() if v})
    if preview:
        post = None
        params["preview"] = True
    return submit(kind, params, lambda k, nm: ltx_workflow(vprompts[k], w, h, frames_list[k], steps, seed, nm),
                  source, scenes=len(prompts), images=images, post=post, sheet=preview, chain=chain and not preview, stills=still_info)


async def read_json(req):
    try:
        d = await req.json()
    except Exception:
        raise web.HTTPBadRequest(text="JSON body required")
    if not isinstance(d, dict):
        raise web.HTTPBadRequest(text="JSON object required")
    return d


def client_ip(req):
    return req.headers.get("CF-Connecting-IP") or req.remote


# ---------- public app ----------
def err(status, msg, **extra):
    return web.json_response({"error": msg, **extra}, status=status)


async def pub_generate(req):
    kind = req.path.strip("/")
    jid = make_job(kind, await read_json(req), source=f"api {client_ip(req)}")
    return web.json_response({"job_id": jid}, status=202)


async def pub_job(req):
    jid = req.match_info["id"]
    j = JOBS.get(jid)
    if not j:
        if find_result(jid):
            return web.json_response({"status": "done", "queue_position": 0, "error": None, "progress": 1.0, "elapsed_s": 0})
        return err(404, "unknown or expired job")
    queued = [k for k, v in JOBS.items() if v["status"] == "queued"]
    return web.json_response({"status": j["status"], "kind": j["kind"], "error": j["error"],
                              "queue_position": queued.index(jid) + 1 if j["status"] == "queued" else 0,
                              "progress": 1.0 if j["status"] == "done" else round(j["done"] / j["total"], 2),
                              "scenes_done": j["clips"], "scenes_total": j["scenes"],
                              "elapsed_s": int((j["finished"] or time.time()) - (j["started"] or j["created"]))})


MIME = {".png": "image/png", ".mp4": "video/mp4", ".webp": "image/webp"}


async def pub_cancel(req):
    """DELETE /jobs/{id} or POST /jobs/{id}/cancel: stop a queued job, or interrupt a running one (the scene being made is dropped)."""
    j = JOBS.get(req.match_info["id"])
    if not j:
        return err(404, "unknown or expired job")
    if j["status"] == "queued":
        j.update(status="cancelled", finished=time.time(), images=None, build=None)
        return web.json_response({"status": "cancelled"})
    if j["status"] == "running":
        j["cancel"] = True
        try:
            async with ClientSession(timeout=ClientTimeout(total=5)) as s, s.post(url("comfy", "/interrupt")):
                pass
        except Exception:
            pass  # the flag alone stops it before the next scene
        return web.json_response({"status": "cancelling"}, status=202)
    return err(409, f"job is already {j['status']}")


async def pub_result(req):
    jid = req.match_info["id"]
    j = JOBS.get(jid)
    if j and j["status"] != "done":
        return err(409, f"job is {j['status']}")
    p = find_result(jid)
    if not p:
        return err(404, "unknown job")
    meta = json.loads(p.with_suffix(".json").read_text()) if p.with_suffix(".json").exists() else {}
    return web.Response(body=p.read_bytes(), content_type=MIME.get(p.suffix, "application/octet-stream"), headers={"X-Seed": str(meta.get("seed", ""))})


async def pub_llm(req):
    tail = req.match_info["tail"]
    if ".." in tail or "%" in tail or "\\" in tail:
        return err(400, "bad path")
    if state["llm_wait"] >= 3:  # callers retry after a tunnel timeout; don't let abandoned requests pile up behind the lock
        return err(429, "busy", retry_after=30)
    payload = await req.read()
    state["llm_wait"] += 1
    try:
        await lock.acquire()
    finally:
        state["llm_wait"] -= 1
    try:
        await ensure("llm")
        async with ClientSession(timeout=ClientTimeout(total=3600)) as s:
            async with s.request(req.method, url("llm", "/v1/" + tail + (f"?{req.query_string}" if req.query_string else "")), data=payload, headers={"Content-Type": req.content_type}) as r:
                resp = web.StreamResponse(status=r.status, headers={"Content-Type": r.headers.get("Content-Type", "application/json"), **cors_headers(req)})
                await resp.prepare(req)
                async for chunk in r.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
        state["last"] = time.time()
        return resp
    finally:
        lock.release()


def queue_info():
    q = sum(j["status"] == "queued" for j in JOBS.values())
    run = next((j["kind"] for j in JOBS.values() if j["status"] == "running"), None)
    name = state["name"]
    return dict(loaded=name if state["ready"] else None, loading=name if name and not state["ready"] else None, busy=lock.locked(), running=run,
                queue_length=q, accepting=q < MAX_QUEUE,
                first_job_slower=not (name == "comfy" and state["ready"]))  # true: the next image/video job first loads the model (about 1-2 min)


async def pub_health(req):
    return web.json_response(queue_info())


def allowed_origins():
    """data/allowed_origins.txt (one origin per line) restricts browser access; absent = any origin (the bearer key is the protection)."""
    f = DATA / "allowed_origins.txt"
    return {l.strip() for l in f.read_text().splitlines() if l.strip()} if f.exists() else None


def cors_headers(req):
    o, allow = req.headers.get("Origin"), allowed_origins()
    if not o or (allow is not None and o not in allow):
        return {}
    return {"Access-Control-Allow-Origin": o, "Vary": "Origin", "Access-Control-Expose-Headers": "X-Seed",
            "Access-Control-Allow-Headers": "Authorization, Content-Type", "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
            "Access-Control-Max-Age": "600"}


@web.middleware
async def cors_mw(req, handler):
    h = cors_headers(req)
    if req.method == "OPTIONS":
        return web.Response(status=204 if h else 403, headers=h)
    try:
        resp = await handler(req)
    except web.HTTPException as e:
        e.headers.update(h)
        raise
    if not resp.prepared:  # streamed responses (LLM) get theirs when created
        resp.headers.update(h)
    return resp


@web.middleware
async def pub_mw(req, handler):
    t = time.time()
    status = 500
    try:
        if req.path != "/health" and not secrets.compare_digest(req.headers.get("Authorization", "").removeprefix("Bearer ").strip().encode("utf-8", "replace"), TOKEN.encode()):
            status = 401
            return err(401, "unauthorized")
        resp = await handler(req)
        status = resp.status
        return resp
    except web.HTTPException as e:
        status = e.status
        raise
    finally:
        REQLOG.append(dict(t=datetime.now().strftime("%H:%M:%S"), ip=client_ip(req), method=req.method, path=req.path, status=status,
                           ms=int((time.time() - t) * 1000)))


# ---------- admin app (localhost only) ----------
@web.middleware
async def admin_mw(req, handler):
    # blocks DNS-rebinding / cross-site calls: only our own origin may talk to the admin API
    if req.host not in (f"127.0.0.1:{ADMIN_PORT}", f"localhost:{ADMIN_PORT}"):
        return err(403, "bad host")
    o = req.headers.get("Origin")
    if o and o not in (f"http://127.0.0.1:{ADMIN_PORT}", f"http://localhost:{ADMIN_PORT}"):
        return err(403, "bad origin")
    return await handler(req)


def lib_items():
    items = []
    for meta in LIB.glob("*/*/*.json"):
        f = next((p for p in meta.parent.glob(meta.stem + ".*") if p.suffix != ".json"), None)
        if not f:
            continue
        try:
            m = json.loads(meta.read_text())
        except Exception:
            m = {}
        items.append(dict(m, kind=meta.parent.parent.name, date=meta.parent.name, file=f.name, bytes=f.stat().st_size))
    return sorted(items, key=lambda i: i.get("created", ""), reverse=True)


def proc_mb(p):
    try:
        pr = psutil.Process(p.pid)
        return round(sum(c.memory_info().rss for c in [pr, *pr.children(recursive=True)]) / 2**20)
    except psutil.Error:
        return 0


async def comfy_gpu():
    if state["name"] != "comfy":
        return None
    try:
        async with ClientSession(timeout=ClientTimeout(total=1.5)) as s, s.get(url("comfy", "/system_stats")) as r:
            d = (await r.json())["devices"][0]
            return dict(name=d["name"], total_mb=round(d["vram_total"] / 2**20), free_mb=round(d["vram_free"] / 2**20))
    except Exception:
        return None


async def adm_state(req):
    vm = psutil.virtual_memory()
    jobs = sorted(JOBS.values(), key=lambda j: j["created"], reverse=True)[:15]
    return web.json_response(dict(
        **queue_info(), token=TOKEN, uptime_s=int(time.time() - state["started"]), idle_s=int(time.time() - state["last"]),
        idle_stop_s=IDLE_S, backend_mb=proc_mb(state["proc"]) if state["proc"] else 0, gpu=await comfy_gpu(),
        system=dict(cpu=psutil.cpu_percent(), ram_used_mb=round(vm.used / 2**20), ram_total_mb=round(vm.total / 2**20)),
        listen=dict(public=f"127.0.0.1:{PORT}", admin=f"127.0.0.1:{ADMIN_PORT}", comfy=f"127.0.0.1:{BACKENDS['comfy']['port']}", llm=f"127.0.0.1:{BACKENDS['llm']['port']}"),
        tunnel=dict(url=state["tunnel_url"], running=bool(state["tunnel_proc"] and state["tunnel_proc"].poll() is None),
                    installed=(BIN / "cloudflared.exe").exists(), push=state["push"], log=list(state["tunnel_log"])[-3:]),
        jobs=[dict(id=j["id"], kind=j["kind"], status=j["status"], error=j["error"], source=j["source"], prompt=j["params"]["prompt"][:80],
                   age_s=int(time.time() - j["created"])) for j in jobs],
        requests=list(REQLOG)[-40:][::-1], library=dict(images=sum(1 for _ in (LIB / "image").glob("*/*.png")), videos=sum(1 for _ in (LIB / "video").glob("*/*.mp4")))))


async def adm_token(req):
    global TOKEN
    new = secrets.token_hex(24)
    write_token(new)
    TOKEN = new
    return web.json_response({"token": TOKEN})


async def adm_library(req):
    return web.json_response(lib_items())


def lib_file(req):
    p = (LIB / req.match_info["kind"] / req.match_info["date"] / req.match_info["name"]).resolve()
    if LIB.resolve() not in p.parents or not p.exists():
        raise web.HTTPNotFound()
    return p


async def adm_file(req):
    return web.FileResponse(lib_file(req))


async def adm_index(req):
    return web.FileResponse(HERE / "dashboard.html", headers={"Cache-Control": "no-store"})  # always show the current page


# ---------- lifecycle ----------
async def on_start(app):
    global queue
    queue = asyncio.Queue()
    app["tasks"] = [asyncio.create_task(reaper()), asyncio.create_task(worker())]


async def on_stop(app):
    for t in app["tasks"]:
        t.cancel()
    await stop()
    if state["tunnel_proc"]:
        state["tunnel_proc"].terminate()


def public_app():
    app = web.Application(middlewares=[cors_mw, pub_mw], client_max_size=64 * 1024**2)
    app.add_routes([web.get("/health", pub_health), web.post("/image", pub_generate), web.post("/video", pub_generate),
                    web.get("/jobs/{id}", pub_job), web.delete("/jobs/{id}", pub_cancel), web.post("/jobs/{id}/cancel", pub_cancel),
                    web.get("/jobs/{id}/result", pub_result), web.route("*", "/v1/{tail:.*}", pub_llm)])
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    return app


def admin_app():
    app = web.Application(middlewares=[admin_mw])
    app.add_routes([web.get("/", adm_index), web.get("/api/state", adm_state), web.get("/api/library", adm_library),
                    web.post("/api/token", adm_token),
                    web.static("/fonts", HERE / "fonts"), web.static("/brand", HERE / "brand"),
                    web.get("/lib/{kind}/{date}/{name}", adm_file)])
    return app


async def main():
    pub, adm = web.AppRunner(public_app()), web.AppRunner(admin_app())
    await pub.setup(); await adm.setup()
    await web.TCPSite(pub, "127.0.0.1", PORT).start()   # never bind 0.0.0.0: the tunnel is the only way in
    await web.TCPSite(adm, "127.0.0.1", ADMIN_PORT).start()
    psutil.cpu_percent()
    start_tunnel()
    print(f"Gateway   http://127.0.0.1:{PORT}  (public side, bearer token)\nDashboard http://127.0.0.1:{ADMIN_PORT}\nClose this window to stop everything.")
    if not os.environ.get("NO_BROWSER"):
        webbrowser.open(f"http://127.0.0.1:{ADMIN_PORT}")
    try:
        await asyncio.Event().wait()
    finally:
        await pub.cleanup(); await adm.cleanup()


if __name__ == "__main__":
    _job = kill_children_on_exit()  # keep a reference alive for the whole run
    for d in ("image", "video"):
        (LIB / d).mkdir(parents=True, exist_ok=True)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
