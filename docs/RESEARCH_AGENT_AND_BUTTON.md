# Research and plan: one connected system (button → server → AI agent → devices)

This note records what was examined, what was wrong, what was changed and why, and what is left. It covers
the merge of `origin/main`, communication between the Raspberry Pi button and the server, speed on the Pi,
speech-to-text quality, loud undistorted output, precise mouse control for small vision models, working memory
for the agent, and security.

Measurements were made on the development PC. Ratios carry over to the Pi 5. Absolute times on the Pi are
roughly 3–4× longer.

---

## 1. How the parts connect

```
 senior ──► button (Raspberry Pi 5, raspberry_pi/)
             │  record → VAD → clean-up → Whisper (local) ──► text
             │  GET  /api/hello?nonce=…   ◄── server proves it knows the pairing token (HMAC)
             │  POST /api/press, /api/utterance {text}          (Bearer key derived from the token)
             │  POST /api/audio?source=button   (only when the Pi heard speech but got no text)
             ▼
          server (helpbutton/server/app.py, FastAPI)
             │  orchestrator: understanding → recipe, or GUI agent (vision model)
             │  GUI agent: observe_screen → model decides → code checks → execute_action → repeat
             ▼  WebSocket /ws/device (challenge–response with the same token)
          device daemons: PC (helpbutton/agents/pc.py), Android phone, mock devices
```

- A spoken reply (`say`) goes back to the Pi as JSON. The Pi speaks it with its own TTS.
- When the reply has `expects_answer` (confirmation, or the agent's `ask_user`), the Pi reopens the microphone
  by itself. The answer is routed to the waiting agent session.
- A button press while the server is working sends `/api/stop`, and the running agent stops.

This whole chain is now covered by an automated end-to-end test. It uses the real server app with a token,
a request from another IP address, a scripted vision model, a mock phone, and the Pi client with scripted
speech (`raspberry_pi/test_rpi_integration.py::test_e2e_button_drives_gui_agent_with_question_and_token`).
A live run also passed: uvicorn server, mock phone over a real WebSocket with the token, and the Pi client.
The confirmed WhatsApp fix completed in 0.76 s.

---

## 2. Merge of `origin/main`

There were two conflicts:

| File | Local side | Incoming side | Resolution |
|---|---|---|---|
| `helpbutton/server/app.py` (startup) | Whisper warm-up at startup | token / API-key warning | Kept both. The warm-up now runs as a background task, so the server accepts connections at once. It is cancelled on shutdown. |
| `helpbutton/server/static/button.html` | speak `guide` steps | render reply only when not recording | Kept both: render and speak, guide steps included, only when not recording. |

Merge commit: `1ff0428`. All tests passed after the merge.

---

## 3. Mouse precision for small vision models

### Why small models miss

1. **They regress numbers.** A 2–8 B model has to output pixel coordinates for a target on a downscaled
   screenshot. Models are much better at choosing from labelled options than at estimating numbers.
2. **Downscaling.** The screenshot is reduced to 1600 px on the long side. On a 4K screen, one model pixel is
   2.4 screen pixels, and small icons are only a few model pixels wide.
3. **DPI mismatch.** If the process is not DPI-aware, Windows scales the coordinates on 125–150 % displays,
   and every click lands off target. Each library (pyautogui, pywinauto) set its own awareness mode,
   depending on which one was imported first.
4. **Wrong element.** The model describes the target correctly but sends the wrong element number.

### What was built

| Technique | Where | Effect |
|---|---|---|
| **Text-anchored tools** `click_text`, `type_into`, `scroll_to_text`, `wait_for_text` | `agent.py` `SMART_TOOLS`, `_smart` | The model copies the visible text. Code finds the element (case- and accent-insensitive, Czech word stems) and clicks its centre, which comes from UI Automation or OCR. No coordinates are involved. |
| **Grid-cell zoom** (choose a cell, do not estimate a number) | `_zoom_grid`, `_grid_crop`, `_pick_cell` | For an x,y click outside every known element: a crop around the point is enlarged and split into a numbered 4×4 grid. The model answers with a cell number. A large cell is split again 3×3. |
| **Snapping** | `_element_in`, `_snap_salient` | The click moves to a known element inside the chosen cell. Otherwise it moves to the nearest real icon, found with classic computer vision (foreground blobs, no extra model). |
| **Grounding check** | `_ground` (existing, now with stem matching) | A click is refused, or redirected, when the element number does not match what the model said it targets. |
| **Per-Monitor-v2 DPI awareness** | `pc_gui.dpi_aware()` runs before pyautogui and pywinauto are imported | Screenshot pixels, UI Automation rectangles and mouse coordinates use the same physical pixels on every monitor. |

The old coordinate zoom (2× crop with a red cross) is kept as a fallback for models that do not support the
structured `ask` call.

Tests: `tests/test_agent_tools.py`. A model that can only name a cell lands within 3 px of a 22 px icon that
the first guess missed by about 65 px. A known element in the cell is used directly, after one question.

---

## 4. Working memory during a task

Small models forget what they found three steps ago (a file path, which window opened) and search again.

- **`remember` tool**: the model stores a short fact.
- **Automatic facts**: results of device tools add facts by themselves, for example `find_files: folder = …`,
  `start_app: window = …`, `list_apps: apps = …`.
- The prompt always contains the section *"Facts you know from earlier in this task"*. It is never shortened,
  even when the small model's context budget forces the step history to be cut.
- At most 10 facts per task, no duplicates. Verification codes are hidden (`redact_codes`) before storing.

Long-term memory (`AgentMemory`, successful step sequences of similar earlier tasks) was already in place and
is unchanged.

---

## 5. Predefined tools without predefined tasks

The smart tools are generic, and nothing in them is tied to a particular task:

| Tool | Arguments | What code does |
|---|---|---|
| `click_text` | `text`, `double` | find the element by visible text, click it. On failure it returns similar labels so the model can correct itself. |
| `type_into` | `field`, `text`, `enter` | find the input box by label (or the only or focused one), type, optionally press Enter |
| `scroll_to_text` | `text`, `direction` | scroll until the text is visible (at most 6 times) |
| `wait_for_text` | `text`, `seconds` | wait until a page shows the text (at most 15 s), checking the current screen first |
| `remember` | `fact` | working memory (section 4) |

Every smart action goes through the same safety checks as a direct click: risky buttons ("Zaplatit", "Odeslat"),
passwords, PINs and codes are refused, and protected apps stay off limits. Small models often write the tool
name as the action (`"action": "click_text"`) and put the text into `target`. `_normalize` repairs that.

---

## 6. Speed

| Where | Problem | Change | Measured |
|---|---|---|---|
| Server `prepare_mic_float32` | 75 Hz high-pass ran as a per-sample Python loop **on the event loop**, so every WebSocket stalled during clean-up | Vectorised (scipy `lfilter`, or a numpy block recurrence when scipy is missing) and moved to a worker thread | 8 s recording: 41 ms → 4.6 ms; identical output (error < 1e-11) |
| Pi, per 30 ms frame | level, zero-crossing rate, click check in pure Python | numpy fast paths, identical results (tested) | 8–19× faster |
| Pi, 48 kHz stereo HAT block | stereo → mono in pure Python | numpy | 429 µs → 15 µs (29×) |
| Pi, non-integer resampling | Python loop | numpy, bit-identical | — |

Earlier STT work (already committed): greedy single-pass Whisper, parallel warm-up, background transcription
during pauses, and skipping a speculative pass when Whisper is busy.

---

## 7. Speech-to-text quality

1. **Aliasing on the server.** Recordings at 44.1 or 48 kHz were resampled with linear interpolation. A 12 kHz
   hiss folded back to 4 kHz at the same level as the speech (+0.2 dB). With band-limited FFT resampling it is
   now −119 dB.
2. **The microphone boost was probably never switched on.** On the WM8731, "Mic Boost" is a one-step volume
   (+20 dB), not a switch, so `amixer sset 'Mic Boost' on` fails with "Invalid command". The codec's power-on
   default also mutes the microphone. The firmware and installer now set the exact driver controls (`cset name='Mic Boost Volume' 1`,
   `name='Mic Capture Switch' on`) and fall back to `sset` if that fails. Without the boost, a senior speaking
   at 30 cm reached the ADC about 20 dB too quietly, and the digital gain also raised the noise.
3. **Clipping is handled automatically.** If more than 1.5 % of the samples clip, the boost is switched off once.
   The digital gain makes up the level, and the speech is no longer distorted. The old code lowered
   `Capture`, which on the WM8731 only affects the line input.
4. **Second opinion.** When the Pi heard at least 0.3 s of speech but got no text, it sends the WAV to the
   server's Whisper (`/api/audio?source=button`) before asking the senior to repeat. The server usually runs on
   a faster machine and can use a larger model.

---

## 8. Loud output without distortion (headphone output driving the speaker)

| Finding | Change |
|---|---|
| WM8731 `Master` 100 % = **+6 dB** headphone-amp gain. At +6 dB a full-scale peak needs about ±2.8 V at the output, but the 3.3 V analog supply allows about ±1.65 V, so loud speech peaks clipped in the analog stage. At 0 dB a full-scale peak is about ±1.4 V and fits. | 100 % now means `Master` **0 dB**, the loudest clean setting. Lower percentages still work as before. |
| "Playback De-emphasis" was **on**. It is a CD-era treble cut (up to −10 dB at high frequencies) that dulls s, š, č. | Switched off. |
| Software loudness drive was +6 dB. | **+9 dB** into the lookahead limiter. Speech is about +7 dB louder than the raw TTS voice, with no clipped samples. Distortion of a 120 Hz tone is under 0.2 %, at 220 Hz 0.01 %. |

Net effect: the speech is louder in the speaker and no longer clips in the HAT's analog stage.

---

## 9. Security review

| Risk | Status |
|---|---|
| **Rogue server on the LAN.** mDNS is unauthenticated, so anyone could announce `_helpbutton._tcp` and collect the button's API key, audio and text. | Fixed. Before the first request to an address, the Pi sends a random nonce to `/api/hello`, and the server answers with `HMAC(token, "server-api|nonce|")`. A server that cannot prove the token gets nothing. If several are announced, the Pi uses the first one that proves the token. Tested with a fake server. |
| Oversized uploads (memory exhaustion) | Fixed. Audio is limited to 8 MB, also checked while streaming (chunked uploads), and returns 413. Sample rate must be 4–96 kHz (400). |
| Oversized sentences | Fixed. `text` is limited to 2 000 characters (422). |
| Token readable by other users on the Pi | Fixed. The installer sets `chmod 600` on `rpi_button.toml` and `helpbutton.toml`. |
| Device impersonation | Already in place: challenge–response on `/ws/device`. |
| Cross-site requests from web pages | Already in place: Host and Origin allowlists, `SameSite=Strict` cookie, login `next` limited to `/` and `/button`. |
| Brute force | Already in place: lockout after 10 wrong keys per address. |
| SSRF from agent web tools | Already in place: fixed service hosts only. |

**Residual risks** (documented, not fixed):

1. **No TLS in the LAN.** A passive listener can read the API key (derived from the token, but replayable) and
   the transcripts. An active man-in-the-middle could relay `/api/hello`. The fix is TLS with a certificate
   pinned through the token (section 10).
2. **Loopback is trusted.** Requests from `127.0.0.1` need no key. Do not put a reverse proxy on the server
   machine that forwards outside traffic to `127.0.0.1` without a token.
3. **No token = open API.** With an empty `[helpbutton] token` on a non-loopback host, the server logs a warning but
   accepts everyone.

---

## 10. Next steps (prioritised)

1. **UI Automation actions instead of mouse clicks** on Windows: use the element's `Invoke`/`Toggle`/`Value`
   pattern for standard controls. This makes the click independent of pixels, focus and overlapping windows.
   Fall back to the mouse for owner-drawn content.
2. **TLS with a pinned certificate.** The server generates a self-signed certificate. The Pi and the daemons pin
   its fingerprint, which the server proves with the token, the same way as `/api/hello`.
3. **Larger STT model on the server** (`large-v3-turbo` on a GPU) for the second opinion and for browser
   push-to-talk.
4. **Acoustic echo cancellation** on the Pi (speexdsp or WebRTC AEC), so the senior can interrupt by voice and
   not only with the button.
5. **Verified clicks**: compare the screen before and after each click, and tell the model "nothing happened"
   one step earlier than today's fingerprint check.
6. **Per-application skills** recorded from successful runs (the run recorder already stores them), reviewed
   by a person before they are used as examples.

---

## 11. Test inventory

| Suite | New tests | Covers |
|---|---|---|
| `tests/test_agent_tools.py` | 19 | smart tools, refusal of risky buttons and codes, working memory, grid zoom, snapping, normalisation |
| `tests/test_server_extras.py` | 9 | `/api/hello` proof (same on server and Pi), limits, second-opinion source, vectorised filter, aliasing |
| `raspberry_pi/test_rpi_integration.py` | 9 | end-to-end chain with token, fake-server refusal, second opinion, numpy = pure Python, WM8731 mixer, clipping response |

`pytest` now runs the server and the Pi suites together (`testpaths = ["tests", "raspberry_pi"]`):
**274 passed**. The Pi suite also passes with numpy and scipy blocked: 75 passed, 7 skipped.
