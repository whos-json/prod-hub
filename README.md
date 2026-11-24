# Local AI Gateway

Double-click `start.bat`. One console window runs everything; closing it stops every server (gateway, ComfyUI, llama-server, tunnel).
Dashboard: http://127.0.0.1:8001 (local only, never tunnelled): key, load, requests, library.

## Layout
```
start.bat                 launcher (one window)
app/                      gateway.py (API + dashboard server), dashboard.html, fonts/
tests/                    test_gateway.py  (smoke test against a running gateway)
engine/
  comfyui/                ComfyUI + ComfyUI-GGUF + its own .venv (Intel XPU PyTorch)
  llama-server/           llama.cpp (text model)
  extra_model_paths.yaml  points ComfyUI at ../models
models/
  unet/                   flux1-schnell-Q4_K_S.gguf              (images)
  clip/                   t5xxl_fp8_e4m3fn, clip_l               (text encoders)
  vae/                    ae.safetensors
  checkpoints/            ltxv-2b-0.9.8-distilled-fp8            (video)
  llm/                    Qwen2.5-VL-7B Q4_K_M + mmproj          (text/vision)
bin/                      cloudflared.exe
data/                     token.txt (API key), library/<image|video>/<date>/, logs/, tunnel_token.txt, public_url.txt
```
`engine/`, `models/`, `bin/` and `data/` are git-ignored; everything else is the project.

## Render
Set two environment variables on the Suncatcher service:
- `GATEWAY_URL`: the public tunnel URL shown on the dashboard
- `GATEWAY_TOKEN`: the key from the dashboard (Regenerate invalidates the old one at once)

## API (bearer token on everything except /health)
- `POST /image` `{prompt,width,height,steps,seed}` -> `202 {job_id}`. Default 768x960, 4 steps (~50 s). Sizes: multiples of 16, 256..1280, <= 1.1 MP.
- `POST /video` -> `202 {job_id}`. One clip `{prompt,width,height,frames,steps,seed}` (default 512x896, 121 frames = 5 s @ 24 fps, ~90 s; frames = 8n+1; <= 0.6 MP), or a **storyboard** `{scenes:[...], ...}`: 1..8 scenes of 0.5..4 s (12 s total) made into one MP4 with edits and sound.
  - Scene: `{prompt, seconds, image?, text?, transition?}`. `prompt` says what moves or happens. `image` (base64 or data URL, JPEG/PNG/WEBP, <= 8 MB) **animates a photo**: it is the first frame, then it moves (image-to-video; metadata is stripped and the upload is not kept). `text` (<= 80 chars) is a caption burned in. `transition` overrides the default for the cut *into* this scene.
  - Edits: `style` (<= 300 chars, put in front of every scene prompt; one seed is shared so the look stays consistent), `transition` = `cut | fade | dip | wipe` (default `fade`), `transition_ms` (100..1500, default 400), `fade_in_ms` (default 0), `fade_out_ms` (default 400).
  - Sound: `voiceover` (text, <= 600 chars; offline Windows voice, `voice` can name one, e.g. `Microsoft Zira Desktop`), `music` (a mood: calm, warm, upbeat or moody, simple synthesised music), `music_volume` (0..1, default 0.25; music is ducked under speech and fades out). Result is H.264 + AAC. A long voiceover holds the last frame (up to 4 s).
  - Quality modes (read this for process videos such as "making coffee"): the video model cannot reliably act out a described action from words (liquids, hands, people look wrong). **`"stills": true`** fixes that: each scene first becomes a Flux picture (photoreal, controllable, ~45 s), then that picture is animated (~50-80 s), so every step looks right and matches its caption. **`"continuity": true`** instead starts each scene from the previous scene's last frame (colour-matched to scene 1 so it does not drift); good for one continuous place, weaker for distinct steps. Write scene prompts as concrete descriptions ("dark brown espresso pouring into a small glass cup"), not instructions.
  - `"preview": true` (same body) returns one PNG contact sheet, a small frame per scene, in about 10 s per scene, to approve scenes before the real render. `DELETE /jobs/{id}` cancels.
  - Limits: up to 10 scenes and 20 s total. Time (this iGPU, measured): a 3 s clip takes about 40 s warm at 512x896 (default) or 55 s at 576x1024; a 15 s, 5-scene `stills` video takes about 8.5 min including the first model load; a Flux picture about 50-70 s. Switching between image and video jobs reloads models, so a batch of one kind is faster than mixing.
  - Memory: the iGPU shares RAM with Windows, so ComfyUI is started with a 3 GB reserve, and a scene that still runs out of memory restarts ComfyUI and is retried once. If you see slowdowns or errors, close other GPU-heavy apps (browsers running tests, video calls).
  - The single-clip form also takes `image`, `text`, `voiceover` and `music`. A plain scene without `stills` takes roughly 20-80 s.
- `GET /jobs/{id}` -> `{status: queued|running|done|error, queue_position, progress (0..1), scenes_done, scenes_total, elapsed_s, error}`
- `GET /jobs/{id}/result` -> PNG / animated WEBP (also served after a restart)
- `ANY /v1/...` OpenAI-compatible text model (Qwen2.5-VL 7B)
- `GET /health` -> `{loaded, busy, running, queue_length, accepting}`

Browsers may call the API directly (CORS is on for any origin, preflight needs no key). Anyone who can open that web page can read the key from it, so prefer server-side calls where you can; to restrict browsers to your own sites, list their origins (one per line, e.g. `https://your-app.onrender.com`) in `data/allowed_origins.txt`.

Prompts must say "no text, no letters": Flux cannot spell. Only one model is loaded at a time; the next request for another model swaps it in (first load ~1 min), and an idle model unloads after 10 min.

## Tunnel and keeping the hosted app connected
`bin/cloudflared.exe` starts automatically (free quick tunnel). Its address changes on every launch, so the gateway tells your hosted app the new one itself:
put each hosted app's receiving endpoint (https), one per line, in `data/hosted_app.txt`, e.g. `https://your-app.onrender.com/api/media/gateway-url`. Any number of apps can use the same address and key; browser-based apps can also call the API directly (CORS is on).
On every launch, once the tunnel is up, the gateway POSTs `{"url": "https://….trycloudflare.com"}` to it with `Authorization: Bearer <key>` and retries every 30 s until it gets a 2xx. The app must check the bearer against its `GATEWAY_TOKEN`, accept only `https://*.trycloudflare.com`, and store the address in a database (Render's disk is ephemeral). The dashboard (Connections) shows whether the app was updated.
For a permanent address instead: create a named Cloudflare tunnel, save its token in `data/tunnel_token.txt` and its URL in `data/public_url.txt`.

## Setup on a new machine
1. Python 3.12: `py -3.12 -m venv engine\comfyui\.venv`, then install the Intel XPU build
   (`pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/xpu`), then
   `pip install -r engine\comfyui\requirements.txt gguf aiohttp psutil`.
2. Put ComfyUI (with `custom_nodes/ComfyUI-GGUF`) in `engine/comfyui`, llama.cpp in `engine/llama-server`, the files above in `models/`, and `cloudflared.exe` in `bin/`.

`python tests\test_gateway.py [--llm]` runs the smoke test against a running gateway; `python tests\test_edit.py` checks transitions and audio offline (no GPU); `python tests\test_push.py` checks the address push.
Never commit or share `data/token.txt` or `data/tunnel_token.txt`.
