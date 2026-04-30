# Tachyon Transcripts — Development Log

> **This document MUST be updated after every completed task.** It is the single source of truth for what's done, what's in progress, and what's next. Every agent should read this before starting work and update it when finished.

## Status Summary

| Module | Status | Notes |
|--------|--------|-------|
| Project scaffolding | Done | Directory structure, requirements.txt, bat scripts |
| `config.py` | Done | JSON config with defaults, load/save, pathlib, LoopbackDevice support |
| `capture.py` | Done | WASAPI mic + multi-loopback, resampling, WAV writing, device_manifest.json |
| `transcriber.py` | Done | faster-whisper CUDA, rolling buffer, word timestamps, dynamic speaker labels |
| `hardware.py` | Done | NVML/torch hardware detection + auto model/device recommendation |
| `session.py` | Done | Session lifecycle + thread-safe segment storage |
| `exporter.py` | Done | Markdown transcript generation with Protocol types + versioned export + dynamic audio links |
| `batch.py` | Done | Batch re-transcription with crosstalk suppression + dedup + multi-loopback WAV discovery |
| `ui/wizard.py` | Done | First-run setup wizard + legal consent gate |
| `ui/theme.py` | Done | Sci-fi dark theme: deep blue-teal palette, cyan accents, glow tokens, card states |
| `ui/widgets.py` | Done | Custom widgets: HoverButton, GlowFrame, GradientBar, PulseIndicator, SessionCard |
| `ui/reviewer.py` | Done | Sci-fi reviewer: gradient toolbar, session cards, two-line header, status bar, window icon |
| `ui/tray.py` | Done | Navy/cyan icon with LANCZOS anti-aliasing, module-level create_app_icon() |
| `ui/overlay.py` | Done | HUD edge line, PulseIndicator recording, cyan glow border, 15pt captions |
| `diarizer.py` | Done | Speaker diarization: sliding window embeddings + clustering + timeline majority vote, multi-loopback WAV discovery |
| `main.py` | Done | Entry point, component wiring, lifecycle management |
| `setup.bat` | Done | First-time venv + deps + model download |
| `run.bat` | Done | pythonw launcher (no console) |
| `update.bat` | Done | Lightweight dep updater (no venv/model re-download) |
| Tray icon asset | Done | Generated programmatically in tray.py — no external asset needed |

## Build Order

Per the implementation plan, the build order is:
1. **Core Engine** (Steps 1-3): Scaffolding + Audio Capture + Transcription
2. **End-to-End Output** (Steps 4-5): Session Manager + Markdown Export
3. **UI Layer** (Steps 6-7): System Tray + Caption Overlay
4. **Polish & Packaging** (Steps 8-10): Config + Main + Launchers

## Work Log

### 2026-04-30 — Security hardening pass (supply chain + native hotkey)

**What was done:**
- **HuggingFace model pinning**: every HF model the app loads now goes through a pinned commit SHA so a compromised HF account or registry incident cannot silently swap weights (model loads are pickle-deserialization boundaries — malicious weights = code execution on load).
  - New `src/tachyon/model_pins.py`: single source of truth for `WHISPER_REVISIONS` (large-v3 / medium / small / distil-large-v3), `SPEECHBRAIN_ECAPA_REVISION`, and `PYANNOTE_EMBEDDING_REVISION`. Includes a `whisper_revision()` lookup helper and a header-level recipe for refreshing pins.
  - `src/tachyon/transcriber.py`: `WhisperModel(...)` now passes `revision=` via a `_load_whisper_pinned()` helper that falls back to unpinned with a loud warning if the installed faster-whisper is too old to accept the kwarg.
  - `src/tachyon/diarizer.py`: speechbrain `EncoderClassifier.from_hparams(...)` and pyannote `Model.from_pretrained(...)` go through `_load_speechbrain_pinned()` and `_load_pyannote_pinned()` — same pin-with-fallback pattern.
  - `scripts/download_model.py`: pre-download honours the Whisper pin and prints the short SHA to the user.
  - `setup.bat`: speechbrain pre-download command imports `model_pins` and passes the pinned `revision`.
- **Transitive dependency lock**: added `requirements-lock.txt` generated from a known-good `.venv` via `pip freeze`. `setup.bat` and `update.bat` prefer the lock when present and fall back to the human-curated `requirements.txt` otherwise. Lock file deliberately omits `keyboard` (dropped in the same pass — see below).
- **pyannote.audio install pinning**: `src/tachyon/ui/reviewer.py:_install_pyannote()` now installs `pyannote.audio==3.3.2` (constant `_PYANNOTE_PINNED_SPEC`) instead of the unbounded `pyannote.audio`, eliminating the "compromised PyPI account = arbitrary code execution" path on the on-demand install. The install-prompt also names the pinned version so the user sees what they're agreeing to.
- **First-run wizard system-audio scope warning**: added a yellow warning to the loopback-picker page (`src/tachyon/ui/wizard.py:_render_loopback`) explaining that *all* system audio is captured during recording — including notifications, other apps, and anything spoken aloud near the mic — and that recordings are saved as unencrypted WAV files.
- **Replaced `keyboard` library with native Win32 `RegisterHotKey`**:
  - New `src/tachyon/hotkey.py`: `HotkeyListener` class + `parse_hotkey()` parser. Spawns a daemon thread that registers a single hotkey via `RegisterHotKey(NULL, …)` and pumps Win32 messages with `GetMessageW`. Teardown via `PostThreadMessageW(WM_QUIT)` + `UnregisterHotKey`. Handles modifiers (`ctrl`/`alt`/`shift`/`win` + aliases), letter/digit keys, and named keys (F1–F12, space, enter, esc, arrows, etc.). Falls back to a logged warning if registration fails.
  - `src/tachyon/main.py`: dropped `import keyboard`; `_start_tray_and_hotkey()` now spins up a `HotkeyListener`. `_on_quit()` tears it down before stopping the tray.
  - `requirements.txt`: removed `keyboard>=0.13.5`.
  - `installer/tachyon.spec`: dropped `keyboard` from `hidden_imports` (`pystray._win32` retained).
  - `CLAUDE.md` + `docs/implementation-plan.md`: tech-stack note updated.

**Decisions made:**
- **Pin via fallback rather than hard-fail.** Each pinned-model loader (`_load_whisper_pinned`, `_load_speechbrain_pinned`, `_load_pyannote_pinned`) catches `TypeError` from older library versions that do not accept `revision=` and falls back to an unpinned load with a `logger.warning`. The right fix to a missing pin is upgrading the library; the fallback is so a stale dev environment does not brick the app.
- **pyannote pinned to 3.3.2 not 4.x.** The current diarizer uses `Model.from_pretrained("pyannote/embedding", token=...)` and `Inference(model, window="whole")`, both stable across 3.x. 4.x is the latest on PyPI but introduces API changes that are not pre-tested here. 3.3.2 is the last 3.x release and matches the existing diarizer code.
- **Lock file kept inclusive (pyinstaller, pytest).** Filtering dev-only packages out of the freeze creates two locks to maintain. Kept everything that was actually resolved together; the few extra MB are harmless on a runtime install.
- **`RegisterHotKey` over `pynput`/`global-hotkeys`.** Native Win32 means zero new dependencies, no global keyboard hook (so AV products do not flag it), and the implementation is ~200 lines that fit the one-hotkey use case exactly. `keyboard` was unmaintained since 2019 and was the single biggest reason AV products flagged the PyInstaller bundle.
- **Warning placement on the loopback page, not the consent page.** The consent page already covers legal exposure; the technical "what gets captured" question naturally belongs on the page where the user picks the capture device.

**Verification:**
- `pytest tests/` — all 27 tests pass.
- `python -c "from tachyon.model_pins import whisper_revision; ..."` — pins resolve.
- `HotkeyListener('ctrl+shift+alt+f10', cb).start(); .stop()` — actually claims and releases a Win32 hotkey via the message loop without error.
- `from tachyon import transcriber, diarizer, main, hotkey` — all import cleanly with `keyboard` uninstalled (it's still in the venv from the old install but no code references it anymore).

**Issues encountered:**
- None. Existing `.venv` still has the `keyboard` package installed; users on old venvs will keep it until they re-`setup.bat` against the new lock file. No runtime impact.

### 2026-04-29 — Floating circular Help button in reviewer (replaces toolbar Help)

**What was done:**
- Replaced the toolbar `Help` button with a floating circular button in the reviewer's bottom-right corner:
  - `src/tachyon/ui/reviewer.py`: removed the `Help` `HoverButton` and its tooltip from `_build_toolbar()` (the `config_frame` group now contains only `Open Folder`).
  - Added `_create_help_circle()` that builds a 40×40 `tk.Canvas` with a filled cyan oval (`Color.accent` / hover `Color.accent_hover`), a bold white "?" centered, hand cursor, and a `<Button-1>` binding routed through the existing `_on_help_click()` -> `_open_tutorial(force=True)`.
  - Anchored via `place(relx=1.0, rely=1.0, anchor=tk.SE, x=-16, y=-43)` so the circle hovers 16px from the right edge and ~14px above the 29px status bar (28px bar + 1px divider). Stays glued to the corner across reviewer resize.
  - Preserved tooltip text "Explain this screen" on the new circle.
  - Wired in at end of `_create_window()` after `_update_button_state()` so widget refs already exist.
- Tutorial behavior unchanged: same `_open_tutorial(force=True)` entry, same persisted `reviewer_tutorial_show_on_open` config preference, same in-card "Show this walkthrough when I open Review" checkbox to toggle auto-show back on.
- Tutorial "Find the files" step still anchors to `config_frame` via the existing `help_controls` target key — that frame still contains `Open Folder` after Help removal, so the highlight remains correct.

**Decisions made:**
- Used a `tk.Canvas` rather than a square `HoverButton` to render a true circle (filled oval) instead of approximating one with padding.
- Placed on `self._window` rather than inside the transcript text frame so the button is anchored to absolute window geometry and stays put regardless of paned-window sash drag or speaker panel show/hide.
- Matched canvas `bg` to `Color.bg_surface` (transcript area background) so the canvas square edges blend with the area underneath the circle.
- Kept the existing tooltip copy ("Explain this screen") for continuity with prior behavior.

**Issues encountered:**
- None.

### 2026-04-29 — Stable guided tour overlay (removed transparent multi-window layering)

**What was done:**
- Reworked reviewer tutorial rendering in `src/tachyon/ui/reviewer.py` to avoid transparent multi-`Toplevel` composition:
  - Removed transparent backdrop and transparent highlight `Toplevel` windows from the tour flow.
  - Kept a single floating tutorial card `Toplevel`.
  - Added an in-reviewer high-contrast target highlight built from four border edge frames (no filled overlay).
- Preserved dynamic tour behavior:
  - Existing target-aware step metadata and card placement logic remain.
  - Highlight now follows the same target rectangles as steps change.
  - Debounced `<Configure>` sync remains active for reviewer move/resize updates.
- Reduced churn that can trigger visible flicker:
  - Removed per-sync multi-window lift choreography for backdrop/highlight layers.
  - Kept only card positioning + in-window border updates during sync.
  - Help reopen still lifts the card when already open.
- Synced docs:
  - `docs/architecture.md`: now describes one floating card + in-window highlight (no transparent overlay windows).
  - `docs/implementation-plan.md`: reviewer walkthrough note updated to match stable overlay implementation.

**Decisions made:**
- Used border-only in-window highlighting (4 edge frames) for clarity without obscuring underlying controls.
- Prioritized deterministic rendering stability over translucent effects to avoid compositor flicker on Windows.

**Issues encountered:**
- None.

### 2026-04-29 — Redact HuggingFace token from config logs

**What was done:**
- Added a log-safe `Config.__repr__()` in `src/tachyon/config.py`.
- `hf_token` is now rendered as `"<redacted>"` whenever a populated `Config` object is logged, including startup's `Config loaded: ...` line.
- Config file persistence is unchanged: the token still saves to `config.json` so pyannote can use it, but it should no longer be exposed through dataclass-style config logging.

**Decisions made:**
- Redacted at the `Config` representation layer instead of changing only the startup log call, so future `logger.info("%s", config)` style logging is protected too.

**Issues encountered:**
- Existing logs that already contain a token are not rewritten. Rotate any token that has already appeared in logs or chat.

### 2026-04-29 — Dynamic guided review tour (target-following tutorial sync)

**What was done:**
- Reworked reviewer tutorial into a dynamic guided tour in `src/tachyon/ui/reviewer.py`:
  - Tutorial steps now include target metadata and preferred placement (`left/right/above/below/center`) instead of title/body only.
  - Added reviewer widget target mapping for sessions pane, search, versions, transcript area, toolbar actions, and help controls.
  - Added step-aware card positioning that places the tutorial near its target and clamps inside reviewer bounds.
  - Added lightweight target highlight overlay that moves per step.
  - Added reviewer `<Configure>` sync binding while tutorial is open so backdrop/highlight/card all follow move/resize events.
  - Added close cleanup for sync binding and all tutorial overlay windows.
- Preserved existing product behavior:
  - Help button still opens the walkthrough.
  - Auto-show still respects `reviewer_tutorial_show_on_open`.
  - `Back` / `Next` / `Done` / `X` / checkbox behavior unchanged.
- Synced docs:
  - `docs/architecture.md`: reviewer tutorial now documented as dynamic, target-following, and movement-synced.
  - `docs/implementation-plan.md`: transcript review walkthrough note updated from centered overlay to dynamic guided tour.

**Decisions made:**
- Kept the overlay stack reviewer-scoped (`transient`) to reduce desktop-wide overlap issues with unrelated topmost windows.
- Used a simple highlight overlay and placement heuristics instead of brittle cutout masking to keep tkinter behavior stable.

**Issues encountered:**
- None.

### 2026-04-29 — Overlay placement polish (review tutorial centering + caption bottom anchoring)

**What was done:**
- Reviewer tutorial overlay placement and layering:
  - `src/tachyon/ui/reviewer.py`: changed tutorial rendering from a fixed-geometry popup to a reviewer-scoped overlay model.
  - Added a dim backdrop `Toplevel` sized to reviewer bounds.
  - Added a centered tutorial card `Toplevel` on top of the backdrop, positioned from reviewer `rootx/rooty/width/height`.
  - Preserved existing controls and behavior (`X`, `Back`, `Next`, `Done`, persisted checkbox, Help button trigger).
  - Close path now destroys both overlay windows (backdrop + card) to avoid orphaned UI artifacts.
- Closed-caption default placement anchoring:
  - `src/tachyon/ui/overlay.py`: added `_default_position_active` to distinguish initial default placement from user-dragged runtime placement.
  - `_recalc_collapsed_height()` now re-anchors to bottom-center when using default placement so final position is based on actual collapsed height.
  - Dragging the overlay disables default re-anchoring for that runtime session.
  - Explicit/saved positions (`overlay_position`) remain fully respected.
- Synced docs:
  - `docs/architecture.md`: clarified reviewer tutorial as dimmed centered overlay and caption bottom anchoring after collapsed-height calculation.
  - `docs/implementation-plan.md`: mirrored reviewer overlay and caption anchoring behavior notes.

**Decisions made:**
- Kept tutorial overlay scoped to the reviewer window rather than the full desktop to reduce interaction conflicts with unrelated always-on-top windows.
- Did not auto-hide or force-move the caption overlay when tutorial opens; focused this pass on deterministic reviewer overlay placement and caption default anchoring.

**Issues encountered:**
- None.

### 2026-04-29 — Installer uninstall cleanup (artifact removal + output preservation)

**What was done:**
- Tightened uninstall behavior in `installer/Tachyon.iss`:
  - Added an `[UninstallRun]` `taskkill` step to stop `TachyonTranscripts.exe` before deletion, preventing locked `_internal` files and open log handles from surviving uninstall.
  - Expanded `[UninstallDelete]` cleanup to explicitly remove runtime artifacts commonly left behind in smoke tests: `{app}\_internal`, `{app}\assets`, `{app}\docs`, `{app}\models`, `{app}\config.json`, `{app}\tachyon.log`, desktop/startup shortcuts, and the app Start Menu group.
  - Added `Type: dirifempty; Name: "{app}"` so the install root is removed when nothing preserved remains.
  - Preserved `{app}\output\` recordings/transcripts by design.
- Synced uninstall behavior docs:
  - `installer/README.md` now states uninstall removes program/runtime artifacts and shortcuts, while preserving recordings.
  - `README.md` install section now clarifies uninstall scope as app/runtime files + shortcuts removed, recordings preserved.
  - `docs/architecture.md` Distribution & Packaging section now reflects the process-terminate + artifact-cleanup behavior instead of the older blanket "remove install tree" phrasing.

**Decisions made:**
- Chose data safety over full-folder wipe: preserve user content in app-local `output/` even when it means `{app}` may remain if that folder contains recordings.
- Kept explicit artifact delete entries in Inno script for robustness against stale files not tracked in the install manifest.

**Issues encountered:**
- None.

### 2026-04-29 — Reviewer tutorial copy polish for non-technical users

**What was done:**
- Updated tutorial copy in `src/tachyon/ui/reviewer.py` to remove engineering jargon and focus on clear user outcomes.
- Added a new opening walkthrough step (`Review your transcripts`) so first-run users get context before control-by-control guidance.
- Reworded step titles/bodies to plain language:
  - "Sessions list" -> "Your recordings"
  - "Version selector" -> "Transcript versions"
  - "Re-transcribe" guidance framed as "Clean up a transcript"
  - "Identify Speakers" guidance framed as naming speakers
  - "Open Folder" guidance framed as finding saved files
- Polished tutorial control text:
  - Checkbox: `Show this walkthrough when I open Review`
  - Help tooltip: `Explain this screen`

**Decisions made:**
- Kept behavior unchanged (same persistence key, same auto-show trigger, same modal tutorial structure) and limited this pass to wording/UX clarity.
- Kept button labels (`Back`, `Next`, `Done`, `X`) unchanged for consistency with existing navigation expectations.

**Issues encountered:**
- None.

### 2026-04-29 — Reviewer tutorial overlay (guided walkthrough + persisted preference)

**What was done:**
- Added reviewer tutorial preference persistence:
  - `src/tachyon/config.py`: new config field `reviewer_tutorial_show_on_open` (default `True`).
- Implemented an in-window tutorial flow for transcript review:
  - `src/tachyon/ui/reviewer.py`: `TranscriptReviewer` now accepts `tutorial_show_on_open` + `on_tutorial_preference_changed` wiring from `main.py`.
  - Added a modal-ish tutorial `tk.Toplevel` with step content covering sessions, search, transcript pane, versioning, re-transcribe, speaker ID, editing, and output files.
  - Added tutorial controls: `X`, `Back`, `Next`, `Done`, plus a toggle checkbox (`Show this tutorial when Review opens`).
  - Added a `Help` toolbar button to reopen the tutorial at any time, even when auto-show is disabled.
  - Reviewer now auto-opens the tutorial when the Review window is shown from a hidden state and the preference is enabled.
  - Reviewer hide/close path now closes the tutorial overlay cleanly to avoid orphaned modal windows.
- Wired persistence callback in app controller:
  - `src/tachyon/main.py`: passes config preference into `TranscriptReviewer` and persists changes via new `_on_save_reviewer_tutorial_preference(...)`.
- Synced architectural/spec docs:
  - `docs/implementation-plan.md`: added `reviewer_tutorial_show_on_open` to settings and reviewer capability note.
  - `docs/architecture.md`: added `reviewer_tutorial_show_on_open` to settings and documented reviewer tutorial ownership/behavior.

**Decisions made:**
- Kept tutorial implementation inside `ui/reviewer.py` (no new module) to keep reviewer-specific UX logic co-located and lightweight.
- Used a modal-ish `Toplevel` (`transient` + `grab_set`) for focus, while still providing immediate dismissal via both titlebar close and explicit `X`.
- Auto-show triggers only when Review transitions from hidden to shown, preventing repeated popups during simple `lift()` calls.

**Issues encountered:**
- None.

### 2026-04-29 — Review findings remediation (timestamp alignment + startup/docs sync)

**What was done:**
- **Real-time timestamp alignment**:
  - `src/tachyon/capture.py`: changed `AudioChunk.timestamp` semantics to represent the wall-clock **start** of chunk audio, not enqueue/flush time.
  - `_flush_buffer()` / `_flush_loopback_buffer()` now compute `chunk_start = time.time() - (len(resampled) / TARGET_SAMPLERATE)` before queueing.
  - Added deterministic regression coverage in `tests/test_capture_timestamps.py` proving a 3-second chunk flushed at `t=103` is timestamped at `t=100`.
- **Setup model pre-download parity with runtime**:
  - `scripts/download_model.py` now bootstraps `src/` on `sys.path` and uses `tachyon.hardware.resolve_transcriber_config()` so setup chooses the same model/device policy as runtime.
  - Kept setup resilient: if hardware resolution fails, script falls back to CPU `distil-large-v3` and continues.
- **Startup hardening**:
  - `src/tachyon/main.py`: moved `Transcriber(...)` construction into the guarded `try` in `_load_model_worker()` so initialization failures follow the same tray status/notify failure path.
  - `src/tachyon/ui/tray.py`: wrapped menu refresh, title updates, and notifications in guarded `try/except` blocks so cross-thread tray updates fail soft (warn+continue) instead of risking startup dead-ends.
- **Documentation sync (core + release docs)**:
  - `docs/implementation-plan.md`: updated module tree and startup/model-selection behavior (wizard + tray-first + background model load + auto hardware selection), tray menu details, session/export wording, and transcript/audio naming notes.
  - `docs/architecture.md`: aligned startup flow, reviewer layout, tray behavior, overlay placeholder behavior, added `ui/widgets.py`, fixed session/export responsibility wording, and refreshed installer notes (`assets/icon.ico`, `installer/hooks/hook-webrtcvad.py`).
  - `README.md`: updated troubleshooting to reflect tray-visible model-load status (not silent blocking).
  - `installer/README.md`: removed stale “blocks silently” wording and fixed output-folder preservation wording/checklist.
  - `CHANGELOG.md`: populated `[Unreleased]` with these fixes and removed stale “no tests” / silent-load known-issue statements.

**Verification:**
- `.venv\\Scripts\\python -m pytest tests -q` → **27 passed**.
- `.venv\\Scripts\\python -m py_compile src\\tachyon\\capture.py src\\tachyon\\main.py src\\tachyon\\ui\\tray.py scripts\\download_model.py tests\\test_capture_timestamps.py` → pass.
- IDE lint check on edited Python files → no errors.

**Issues encountered:**
- `pytest` was not available in the venv. Installed via `requirements-dev.txt` before running tests.

### 2026-04-29 — README legal section: stronger non-lawyer disclaimer

**What was done:**
- Rewrote the **Legal** section of `README.md` to lead with an
  explicit "we are not lawyers, nothing here is legal advice"
  disclaimer, given the app is being released publicly. Removed the
  prior paragraph that listed specific US states and named GDPR /
  PIPEDA / etc. by jurisdiction — that read like advice, and the
  longer overview already lives in `docs/LEGAL.md` with its own
  non-advice header.
- The new section says, plainly: laws vary by country, state, and
  municipality and change over time; the user is solely responsible
  for checking what applies to them; by using the software they
  accept that responsibility and that the authors disclaim
  liability.
- Tightened the top-of-README legal callout to match — added "we
  are not lawyers and nothing in this project is legal advice" and
  reframed the link to LEGAL.md as "a non-lawyer overview of the
  questions to ask" rather than "the details," so the doc isn't
  implied to be authoritative.
- `docs/LEGAL.md` was left as-is — it already opens with "This
  document is not legal advice" and explicitly recommends
  consulting a lawyer.

**Files changed:**
- `README.md` — top legal callout + `## Legal` section.

### 2026-04-29 — Caption overlay placeholder (no more idle pinstripe)

**What was done:**
- The caption overlay collapses to a ~1px stripe under the title bar
  whenever the line buffer is empty — which is the case at app launch
  before any recording, and briefly at the start of every new
  recording (after `clear_history()` clears the buffer). That stripe
  was hard to grab for dragging and visually noisy without conveying
  any state.
- Added a muted-color placeholder that fills the body whenever
  `_lines` is empty:
  ```
  Tachyon — live captions will appear here while recording.
  Drag to reposition  ·  Right-click the tray icon to control.
  ```
  Rendered in `Color.fg_muted` so it reads visually as "waiting"
  rather than as a real caption.
- Factored the height-recalc logic out of `_update_collapsed_display`
  and `_collapse` into a single `_recalc_collapsed_height` helper.
  `_show_placeholder` only touches text/color so it can be called
  before the window has been positioned, and the init sequence now
  shows placeholder → applies position → recalcs height in that order
  so the bottom-margin offset is right on first paint.
- `_clear_history_impl` now reinstates the placeholder instead of
  leaving the body empty.

**Why this matters:**
- Smoke testing on Windows (and especially in the VM where startup is
  long) revealed the empty overlay as a confusing artefact: a tall,
  draggable bar would be expected, but instead the user saw a sliver
  that looked broken. The placeholder also serves as a discoverability
  cue for the drag and tray-menu controls.

**Files touched:**
- `src/tachyon/ui/overlay.py` — `_PLACEHOLDER_TEXT` module constant;
  `_show_placeholder` / `_recalc_collapsed_height` helpers; init,
  `_update_collapsed_display`, `_clear_history_impl`, and `_collapse`
  rewired to use them.

### 2026-04-29 — Tray-first startup with live model-load status

**What was done:**
- Reordered startup so the system-tray icon comes up **before** the
  Whisper model loads, instead of after. On first-run / no-cache
  machines (and especially in VMs with no GPU) the model download +
  CPU init can take several minutes; the old flow gave the user a
  silent, invisible app for that whole window and many users assumed
  the install had failed.
- `TrayIcon` now exposes `set_status(text)` and `set_model_ready(bool)`.
  Status shows as a disabled item at the top of the menu and is
  mirrored into the icon hover tooltip. "Start Recording" stays
  disabled until the model is ready; "Quit" is always available so
  users can bail out cleanly.
- `App._post_wizard_startup` now starts the tray + hotkey immediately
  with status `"Loading transcription model… (first run downloads
  ~600 MB)"`, then hands the model load off to a daemon thread
  (`_load_model_worker`). On success the status clears and recording
  is enabled. On failure the status flips to `"Model failed to load
  — see tachyon.log"` and a balloon notification fires.
- Added a 30-second heartbeat (`_model_load_heartbeat`) that emits a
  `"Model load still in progress (Ns elapsed)..."` log line while the
  load is running. Hugging Face's downloader reports progress via
  tqdm (not via `logging`), so without this the log went silent for
  minutes during the first-run download — making the app appear hung
  even when it was making forward progress.

**Why this matters:**
- Smoke-testing on a Windows VM (no GPU → CPU + `distil-large-v3`
  int8 path, no model cached) showed the app appearing dead for 5+
  minutes with no tray icon and no log activity past the
  `huggingface_hub` `hf_xet` fallback warning. The old "load model,
  then start tray" ordering meant tray creation was gated behind a
  multi-minute network operation, with no kill switch short of Task
  Manager.

**Files touched:**
- `src/tachyon/ui/tray.py` — `_status_text`/`_model_ready` fields;
  status item rendered atop `_build_menu`; `set_status`/`set_model_ready`
  methods; tooltip update via `self._icon.title`.
- `src/tachyon/main.py` — `_post_wizard_startup` rewritten to be
  tray-first; new `_load_model_worker` and `_model_load_heartbeat`.

**Open follow-ups:**
- The HuggingFace download itself is still slow in VMs even on a
  fast link — `hf_xet` would help but isn't bundled. Consider adding
  it to `requirements.txt` so the bundled binary uses the faster
  Xet protocol for the first-run download.
- Wizard's final page should probably *force* (or strongly default)
  the "Pre-download model" step so first-run users don't hit the
  download path at all.

### 2026-04-29 — Release-readiness pass: stale URLs, repo hygiene, installer build fixes

**What was done:**

Pre-release audit pass on top of the 2026-04-22 v0.1.0 work. Found and fixed the issues that would have hit a public user or a clean-machine builder.

- **Stale GitHub URLs (release blocker)**. The 2026-04-22 port updated the README's GitHub URL but missed two other files that still pointed at the old repo path — these would 404 for any user clicking through:
  - `installer/Tachyon.iss` — `MyAppURL` (visible in Add/Remove Programs publisher metadata)
  - `.github/ISSUE_TEMPLATE/config.yml` — both contact links ("Recording Legality" and "Troubleshooting") shown on every new issue

- **Repo hygiene**. `.claude/settings.local.json` was tracked in git and contained the prior project tree's absolute paths (`C:\Users\PC\Documents\GitHub\TachyonTranscripts\`) — leaks a previous user's directory layout in a public repo and is per-machine settings that shouldn't be shared at all. Removed from tracking via `git rm --cached`, added `.claude/` to `.gitignore`. File preserved on disk locally.

- **Installer build pipeline fixes (smoke-test prerequisites)**. While building the v0.1.0 installer for the first time on this tree, hit two real bugs in the build pipeline:

  1. **`installer/build_installer.bat` did not find Inno Setup installed per-user.** Script searched `where iscc`, `%ProgramFiles(x86)%\Inno Setup 6\`, and `%ProgramFiles%\Inno Setup 6\` — but Inno Setup installs to `%LocalAppData%\Programs\Inno Setup 6\` when the user declines admin elevation (which is most users). Added that path to the search list.

  2. **PyInstaller webrtcvad hook crashed the build.** `pyinstaller-hooks-contrib`'s bundled `hook-webrtcvad.py` unconditionally calls `copy_metadata('webrtcvad')`, but our `requirements.txt` pins `webrtcvad-wheels` (the prebuilt-binary fork — same Python module name, different pip distribution name). `copy_metadata('webrtcvad')` raises `PackageNotFoundError` and aborts PyInstaller before the bundle is produced.
     - **Fix**: created local override `installer/hooks/hook-webrtcvad.py` that tries both distribution names and silently falls back to no metadata if neither resolves. The `webrtcvad` module itself never reads its own metadata at runtime, so an empty `datas` list is harmless.
     - **Wired**: `installer/tachyon.spec` now sets `hookspath=[str(PROJECT_ROOT / "installer" / "hooks")]`. PyInstaller resolves user hooks before contrib hooks, so the local override wins.

  3. **Inno Setup deprecation warning**: `ArchitecturesInstallIn64BitMode=x64` is deprecated in Inno 6.x; auto-substitutes to `x64os` with a build warning. Changed to `x64compatible` (broader — covers ARM64 emulating x64). Cosmetic but eliminates the warning.

- **Verified end-to-end build**. `installer\build_installer.bat` now produces `installer\dist\TachyonTranscripts-Setup-0.1.0.exe` (~1.04 GB, ~6 min build time) with no warnings beyond the unsigned-installer caveat.

**Decisions made:**
- Kept `debug.bat` tracked. The pre-release audit subagent flagged it as accidentally committed, but it's a documented dev-launcher per CLAUDE.md (alongside `run.bat`/`setup.bat`/`update.bat`). Its `.venv` reference is correct for a developer checkout.
- Local `installer/hooks/` directory chosen over patching the venv's contrib hook in place. Patching the venv is invisible, doesn't survive `pip install --upgrade`, and would silently break for any future contributor.
- The webrtcvad metadata fallback returns `[]` rather than synthesizing fake metadata. Verified nothing in our code calls `importlib.metadata.version('webrtcvad')`, and the contrib hook's only purpose is to ship the dist-info for runtime metadata lookups we don't perform.

**Issues encountered:**
- First build attempt had double-clicked `.bat` window closing instantly on PyInstaller error — masking the real failure (the webrtcvad hook). Future builds should be invoked from an already-open shell, or the script should `pause` on error paths. Filed as nice-to-have polish for v1.1.

**What's next:**
- Smoke-test `TachyonTranscripts-Setup-0.1.0.exe` on a clean Windows 10/11 Hyper-V VM against the 9-item checklist in `installer/README.md`. This is Task #6 from the 2026-04-22 plan and is the last gate before tagging v0.1.0.
- After successful smoke test, commit + tag + cut the GitHub release.



**What was done:**
- **Scaffolding**: Created full project structure, `requirements.txt` (sounddevice, faster-whisper, numpy, pystray, Pillow, keyboard, soundfile, soxr), `setup.bat`, `run.bat`, `__init__.py` files
- **`config.py`**: Dataclass-based config with JSON persistence, load/save with defaults merge, overlay_position tuple/list conversion, `get_output_path()` helper
- **`capture.py`**: Full WASAPI audio capture — device enumeration, dual streams (mic + loopback), native rate capture with soxr resampling to 16kHz, ~3s chunk buffering, WAV file writing, graceful loopback fallback
- **`transcriber.py`**: faster-whisper integration with CUDA float16, rolling buffer (1s overlap prepended to each chunk), word_timestamps for precise boundary trimming, speaker label mapping, daemon worker thread with clean stop
- **`session.py`**: TranscriptSegment dataclass, Session class with thread-safe segment storage (threading.Lock), wall-clock tracking, get_recent/get_all accessors
- **`exporter.py`**: Markdown export with Protocol-based typing (decoupled from Session), per-session folder creation, timestamped speaker-labeled transcript, audio file links
- **`ui/tray.py`**: pystray system tray with Start/Stop Recording, Show/Hide Captions, Set Output Folder, Quit. Programmatic 64x64 icon via Pillow with recording indicator dot. tkinter file dialog for folder picker.
- **`ui/overlay.py`**: Transparent always-on-top tkinter window, bottom-center positioning, queue.Queue polling via root.after(100ms), last 4 caption lines, draggable, Segoe UI 14pt white on #1a1a1a, -toolwindow to hide from Alt-Tab
- **`main.py`**: App class wiring all components. Startup: load config → load model → start tray thread → register hotkey → overlay mainloop. Recording: create session → create output dir → start capture → start transcriber. Stop: stop capture → stop transcriber → export markdown. Quit: stop recording if active → stop tray → destroy overlay.
- **CLAUDE.md**: Agent rules requiring doc review before code changes, doc updates after work
- **docs/architecture.md**: Full system design with data flow diagram, module responsibilities, threading model, output layout
- **docs/implementation-plan.md**: Updated with mitigations for WASAPI, chunk boundaries, thread safety, and audio storage/post-processing section

**Decisions made:**
- `transcriber.py` imports `AudioChunk` from `capture.py` rather than defining its own copy — single source of truth
- Tray icon generated programmatically (no `assets/icon.png` needed)
- `exporter.py` uses Protocol types for loose coupling with Session/TranscriptSegment
- Overlay hidden from Alt-Tab via `-toolwindow` attribute
- `faster_whisper` import deferred to `load_model()` to keep module import fast
- `main.py` creates the session output directory before passing `audio_dir` to capture, ensuring WAV files land in the right place

**Issues encountered:**
- None — clean build

**What's next:**
- Test end-to-end on target hardware (2080 Ti)
- Test hotkey registration and overlay toggle
- Consider adding log file output (currently stdout only)

### 2026-03-16 — Fix WASAPI loopback capture

**What was done:**
- **Root cause 1**: `sounddevice`'s `WasapiSettings` does not have a `loopback` parameter in any version (0.5.5 is latest). The bundled PortAudio DLL lacks WASAPI loopback support entirely (GitHub issue #281).
- **Root cause 2**: Device resolution bug — `_resolve_device` for output used sounddevice's default (MME device), found it wasn't WASAPI, and fell back to the **first** WASAPI output device (LG ULTRAWIDE monitor) instead of the **actual** default output (EDIFIER speakers). Loopback captured from a device with no audio playing through it.
- **Fix**: Replaced loopback capture with `PyAudioWPatch` and rewrote device resolution.
- **Changes**:
  - `capture.py`: Import `pyaudiowpatch`. Loopback device resolution now uses `PyAudio.get_host_api_info_by_type(paWASAPI)` → `defaultOutputDevice` to find the correct WASAPI default output, then matches it to a loopback device via `get_loopback_device_info_generator()`. Removed `output_index` from sounddevice resolution (no longer needed). New `_loopback_pyaudio_callback` method with PyAudio callback signature.
  - `requirements.txt`: Added `PyAudioWPatch>=0.2.12`
- **Verified**: PyAudioWPatch correctly resolves to EDIFIER speakers (actual default output) instead of LG ULTRAWIDE (wrong device)

**Decisions made:**
- Keep sounddevice for mic capture (works fine), use PyAudioWPatch only for loopback — minimizes changes
- Loopback device resolution is entirely handled by PyAudioWPatch now — sounddevice's `_resolve_device` is no longer used for output
- Old sounddevice-style `_loopback_callback` removed, replaced with `_loopback_pyaudio_callback` matching PyAudio's callback signature (`bytes` input → numpy conversion)

**Issues encountered:**
- WASAPI loopback only delivers data when audio is actively playing on the device — test captures time out when nothing is playing (expected behavior)

### 2026-03-16 — Fix blank transcripts (CUDA runtime missing)

**What was done:**
- **Root cause**: `cublas64_12.dll` not found at runtime. CTranslate2 4.7.1 loaded the model onto the GPU successfully (driver-level operation) but crashed on every `transcribe()` call because cublas (needed for matrix math) was not installed. The `_process_chunk` exception handler silently caught the error on every audio chunk, so no segments were ever produced.
- **Fix**: Installed `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` pip packages. Added `_register_cuda_dll_dirs()` to `main.py` that calls `os.add_dll_directory()` for nvidia pip package bin directories before any CUDA library is loaded.
- **Changes**:
  - `main.py`: Added `_register_cuda_dll_dirs()` at module level, runs before any imports that touch CUDA
  - `requirements.txt`: Added `nvidia-cublas-cu12`, `nvidia-cudnn-cu12`
- **Verified**: Successfully transcribed 27s of captured system audio — 4 segments with word timestamps

**Decisions made:**
- Use pip-installed CUDA runtime packages rather than requiring a system-wide CUDA toolkit install
- DLL directory registration happens at module-load time in main.py (earliest possible point)

**Issues encountered:**
- mic.wav from the test session was all zeros (max amplitude 0.0) — mic capture may have a separate issue, or the user was not speaking during the test

### 2026-03-16 — Enhanced caption overlay

**What was done:**
- **Title bar**: Added custom title bar (#111111) with "Tachyon" drag handle, expand/collapse toggle (▲/▼), and close button (✕) with hover effects (red for close, dark gray for expand)
- **Close button**: Clicking ✕ hides the overlay and fires an `on_close` callback to `main.py`, which syncs `_captions_visible = False` and updates the tray menu to show "Show Captions". Clicking "Show Captions" in the tray re-opens it.
- **Expand/collapse**: Toggle between compact 4-line view and a 650×400 scrollable transcript. Expanded view uses a `tk.Text` widget with scrollbar, speaker names colored (blue for "You", orange for "Them"). Screen bounds check prevents window going off-screen bottom.
- **Scrollable transcript with auto-scroll**: Full segment history maintained in `_all_segments`. In expanded mode, new segments are appended incrementally (not full rewrite). Auto-scrolls to bottom unless user has manually scrolled up. Scroll position detected via `yscrollcommand` callback.
- **Thread-safe show/hide/toggle**: All visibility methods now schedule on the tkinter thread via `root.after(0, ...)` instead of touching widgets directly from tray/hotkey threads.
- **Clear history**: New `clear_history()` method resets all segments between recording sessions. Called from `main.py._on_start_recording()`.
- **Drag improvements**: Drag now uses `x_root`/`y_root` for more reliable repositioning. Drag bound to title bar and caption label only (not text widget, which needs mouse for scrolling).

**Changes:**
- `ui/overlay.py`: Complete widget rework — replaced single Label with main_frame → titlebar (drag label + expand btn + close btn) + caption_label (collapsed) + expanded_frame (Text + Scrollbar). New state: `_all_segments`, `_expanded`, `_user_scrolled_up`, `_on_close`. Thread-safe visibility via `_show_impl`/`_hide_impl`/`_toggle_impl`.
- `main.py`: Pass `on_close=self._on_overlay_closed` to CaptionOverlay. New `_on_overlay_closed()` syncs captions state with tray. Call `_overlay.clear_history()` at start of each recording session.

**Decisions made:**
- Title bar buttons use `Label` widgets with `bind("<Button-1>")` instead of `tk.Button` for full visual control (no borders, custom hover colors)
- Expanded view incrementally appends segments rather than rewriting entire text widget — better performance with long transcripts
- `_all_segments` is never truncated (full history for expand view); `_lines` keeps only last 4 (collapsed view)
- No changes to `tray.py` — existing "Show/Hide Captions" toggle already works correctly with the new `on_close` callback flow

**Issues encountered:**
- None

### 2026-03-16 — Fix mic capturing from wrong device (silent mic.wav)

**What was done:**
- **Root cause**: Same bug pattern as the loopback fix. `sd.query_devices(kind="input")` returns an MME device (`[1] Headset Microphone (Arctis 7 Chat)`), not WASAPI. Since it's not in the WASAPI device list, `_resolve_device` fell back to the **first** WASAPI input device: `[32] Headset Microphone (Oculus Virtual Audio Device)` — a virtual Oculus device with no real microphone. The actual working mic is `[33] Headset Microphone (Arctis 7 Chat)` (WASAPI).
- **Fix**: When the system default is not WASAPI, `_resolve_device` now does a **name match** first — it looks for a WASAPI device whose name contains the default device's name. This finds the correct WASAPI version of the same physical device. Only falls back to "first WASAPI device of correct kind" if no name match exists.
- **Verified**: After fix, device `[33] Arctis 7 Chat` is correctly selected and captures audio (max amplitude 0.003571 in 2s test).

**Changes:**
- `capture.py`: `_resolve_device` non-WASAPI fallback now does two passes: (1) name-match against the default device name, (2) fall back to first WASAPI device if no match.

**Decisions made:**
- Name matching uses `default_name in dev["name"]` (substring) since sounddevice truncates long device names in MME mode but WASAPI shows the full name

### 2026-03-16 — Microphone selection via tray submenu

**What was done:**
- Added "Set Microphone >" submenu to the system tray context menu listing all WASAPI input devices
- First item is "System Default" (maps to `None`), followed by each WASAPI input device by name
- Currently selected device is indicated with a `*` prefix
- Selecting a device updates `config.json` immediately; takes effect on the next recording start (no hot-swap mid-session)
- Initial selection loaded from config on startup so it persists across restarts

**Changes:**
- `ui/tray.py`: New constructor param `on_set_mic_device`, new state `_current_mic`, new methods `set_mic_device()`, `_handle_set_mic_device()`, `_build_mic_submenu()`. Imports `AudioCapture` from `tachyon.capture` for device enumeration.
- `main.py`: Passes `on_set_mic_device=self._on_set_mic_device` to TrayIcon, calls `set_mic_device()` with initial config value. New `_on_set_mic_device()` method saves device to config.

**Decisions made:**
- Uses `*` prefix for selected device instead of `✓` since pystray has no native checkmark and `*` is more universally readable
- Device enumeration happens each time the menu is opened (via `_build_menu`) so newly connected devices appear without restart
- Submenu placed between separator and "Set Output Folder..." in the menu layout

### 2026-03-16 — Post-recording re-transcription + transcript review UI

**What was done:**
- **Timestamp bug fix** (`transcriber.py`): Segment timestamps were absolute `time.time()` epoch values instead of session-relative offsets, producing timestamps like `[492689:58:57]`. Added `_session_start_time` field and `set_session_start_time()` method. Timestamps now computed as `(chunk.timestamp - session_start_time) + word_offset`. `main.py` sets the session start time before starting the transcriber.
- **Versioned export** (`exporter.py`): Added `discover_versions()` to find all `transcript*.md` files in a session, `next_version_number()` to determine the next version, `export_transcript_versioned()` to write `transcript_v{N}.md` with batch header, and `load_transcript_from_markdown()` to parse transcript markdown back into segments for the reviewer UI.
- **Batch re-transcription** (`batch.py`): New module with `BatchConfig`, `BatchProgress`, and `BatchTranscriber` classes. Processes saved WAV files with enhanced settings: `beam_size=5`, `vad_filter=True`, `condition_on_previous_text=True`, full file processing (no 3s chunking). Includes crosstalk suppression (RMS energy comparison between channels) and segment deduplication (overlapping time + text similarity). Progress callback for UI updates. Cancellable via `threading.Event`.
- **Transcript review UI** (`ui/reviewer.py`): New `TranscriptReviewer` class — a tkinter `Toplevel` window with: left panel session list (scans output dir for `YYYY-MM-DD_HHMMSS` folders), right panel transcript viewer with speaker-colored text and timestamps, version dropdown to switch between original and batch transcripts, bottom bar with re-transcribe button (with cancel support), progress bar, status label, and open-folder button. Session discovery via `discover_sessions()` module-level function.
- **Tray integration** (`ui/tray.py`): Added "Review Transcripts" menu item (disabled during recording), `on_review` callback, and `set_batch_running()` to disable "Start Recording" during batch.
- **Main wiring** (`main.py`): Lazy creation of `TranscriptReviewer` on first tray click. Batch re-transcription spawned in a daemon thread sharing the real-time transcriber's `WhisperModel`. Mutual exclusion: recording disables re-transcribe, batch disables start recording. Progress forwarded to reviewer via `root.after()`. Batch thread cleaned up on quit. Exposed `Transcriber.model` property for model sharing.

**Decisions made:**
- Batch transcriber shares the same `WhisperModel` instance — no VRAM duplication, but mutual exclusion required
- Crosstalk suppression uses RMS energy ratio (10% threshold) — simple and effective for typical meeting audio
- Deduplication uses word overlap ratio (60% threshold) with time tolerance (2s) — catches bleed-through without being too aggressive
- Reviewer is a Toplevel window parented to the overlay's Tk root — uses the same main thread for tkinter safety
- Version dropdown uses display names: "Original (real-time)" and "v2 (batch)", "v3 (batch)", etc.

**Issues encountered:**
- None

### 2026-03-16 — Fix batch transcription dropping mic audio

**What was done:**
- **Root cause**: Silero VAD (faster-whisper's `vad_filter=True`) completely fails on quiet mic channels. The mic had RMS 0.005 with brief speech bursts at 0.01-0.02, while system audio had RMS 0.022. VAD grouped everything from 3-33s as one speech region and missed all later speech, including "Irene! Irene!" at 79s. The mic was simply too quiet for VAD's default sensitivity.
- **Diagnosis**: Tested 6 configurations — raw mic with/without VAD, peak normalization, RMS normalization, various VAD thresholds. Found that `vad_filter=False` caught all speech but risks hallucinations on system channel. The winning approach: RMS-normalize audio to 0.05 RMS + VAD with `threshold=0.2`, `min_silence_duration_ms=1000`, `speech_pad_ms=500`.
- **Fix** (`batch.py`):
  - Added `_normalize_rms()` method — scales audio so its RMS equals a target level, clips to [-1, 1]. This puts both channels on the same energy scale regardless of mic gain.
  - `_transcribe_channel()` now normalizes audio before passing to Whisper.
  - Added `vad_parameters` with tuned settings to `model.transcribe()`.
  - New `BatchConfig` fields: `target_rms=0.05`, `vad_threshold=0.2`, `vad_min_silence_ms=1000`, `vad_speech_pad_ms=500`.
  - Added per-channel RMS/peak logging during audio loading.
- **Verified**: Batch now produces 17 segments (vs 12 before), all 4 "You" lines present including "Irene! Irene!". Zero crosstalk false positives.

**Decisions made:**
- RMS normalization chosen over peak normalization — peak normalization barely moved the mic RMS (0.005→0.009) because the mic has one large transient peak. RMS normalization correctly scales to the target.
- VAD threshold 0.2 (vs default 0.5) — more sensitive to speech, works well after normalization. Even lower thresholds (0.05, 0.1) still failed on unnormalized audio.
- `min_silence_duration_ms=1000` prevents merging separate utterances that are <1s apart. `speech_pad_ms=500` adds 0.5s padding to avoid clipping speech boundaries.

### 2026-03-17 — Speaker diarization system

**What was done:**
- **`diarizer.py`** (NEW): Full speaker diarization engine using acoustic features + clustering. Pipeline: load system.wav → RMS normalize → energy-based VAD → windowed feature extraction (MFCCs, pitch via pyin, spectral centroid/bandwidth/rolloff, ZCR — 37 dimensions per window) → StandardScaler → AgglomerativeClustering (ward linkage) with silhouette-based auto speaker count (2-8) → majority-vote timeline smoothing → segment relabeling. Also speaker_map.json persistence (save/load/update).
- **`exporter.py`** (MODIFIED): Added `export_transcript_diarized()` function — like `export_transcript_versioned()` but with "Diarized" version label and a speaker legend at the top listing all detected speakers.
- **`ui/reviewer.py`** (MODIFIED): Added "Identify Speakers" button (purple `#6b4c9a`) in bottom bar. Multi-speaker coloring in transcript display using 8-color palette by order of appearance. Speaker naming dialog (modal Toplevel) showing color dots, duration, sample text, and name entry fields. Diarize state methods (`set_diarize_running`, `update_diarize_progress`, `on_diarize_complete`). Version dropdown now detects diarized versions. Button mutual exclusion between batch and diarize operations.
- **`main.py`** (MODIFIED): Full diarization lifecycle wiring — `_on_diarize()` spawns daemon thread, `_diarize_worker()` runs pipeline + exports, progress forwarding via `root.after()`, speaker naming dialog completion (`_on_save_speaker_names`) re-exports transcript with user names, mutual exclusion with recording and batch, clean shutdown of diarize thread.
- **`requirements.txt`** (MODIFIED): Added `librosa>=0.10.0` and `scikit-learn>=1.3.0`.
- **Documentation**: Updated `architecture.md` (diarizer module, threading model, output layout), `implementation-plan.md` (moved diarization from future to implemented).

**Decisions made:**
- Custom acoustic pipeline (librosa + sklearn) instead of pyannote — no internet, no HuggingFace tokens, no PyTorch dependency for diarization
- Diarization shares thread slot 6 with batch re-transcription (mutually exclusive) even though it doesn't need Whisper, for v1 simplicity
- Speaker numbering by order of first appearance in the audio (stable across runs)
- 8-color palette: blue (You), then orange/green/red/purple/gold/pink/teal for speakers 1-7
- Speaker names persisted in `speaker_map.json` alongside transcripts
- Diarized transcripts overwritten (not new version) when user saves speaker names

**Issues encountered:**
- None

### 2026-03-17 — Diarizer v2: segment-aligned algorithm + inline speaker panel

**What was done:**
- **`diarizer.py`** (REWRITTEN): Replaced windowed pipeline with segment-aligned approach. The v1 algorithm detected only 1 speaker on a 5-speaker recording because: (a) fixed 1.5s sliding windows crossed speaker boundaries creating noisy features, (b) silhouette-based auto-detection always picked k=2 (monotonically decreasing scores), (c) 37-dim features weren't discriminative enough. The v2 pipeline:
  - Extracts one 72-dim feature vector per "Them" transcript segment (aligned to actual speech boundaries)
  - Features: 20 MFCCs median+IQR, delta MFCCs median, pitch median+IQR, spectral centroid/bandwidth/rolloff/ZCR/flatness median+std
  - Elbow detection for auto speaker count (finds where silhouette score decline rate drops off) instead of max-silhouette
  - Weak clustering guard: if k=2 silhouette < 0.3, bumps to k=3 (better to over-split)
  - Removed: `_find_speech_regions()`, `_extract_features()`, `_compute_window_features()`, `_smooth_timeline()` (old windowed methods)
  - Removed DiarizeConfig fields: `window_duration`, `window_hop`, `energy_threshold_ratio`, `min_speech_duration`, `smoothing_window`
  - Direct segment relabeling — no window→segment overlap mapping needed
  - Unclustered segments (too short for features) assigned to nearest clustered neighbor by time
- **`ui/reviewer.py`** (MODIFIED): Replaced modal speaker naming dialog with inline speaker panel. The panel sits between the transcript header and text widget, showing: speaker count header, per-speaker rows with color dot + name + duration + sample text + entry field, Save Names and Close buttons. Panel hidden by default, shown after diarization or via "Edit Speakers" link on diarized versions. Added `refresh_current_version()` public method. Added `_on_edit_speakers_click()` to rebuild panel from saved speaker_map.json.
- **`main.py`** (MODIFIED): Simplified `_on_save_speaker_names()` — removed direct access to reviewer private members (`_selected_session`, `_populate_version_dropdown`, `_version_var`, `_on_version_changed`), now uses `reviewer.refresh_current_version()` public method.

**Decisions made:**
- Segment-aligned features are more robust than windowed because each vector represents exactly one speaker's audio (no boundary crossing)
- Elbow detection preferred over max-silhouette because silhouette scores from single-channel audio tend to decrease monotonically, making max always pick k=2
- 72-dim features (vs 37-dim) include delta MFCCs and spectral flatness which improve voice discrimination
- Inline panel instead of modal dialog keeps the user in context — they can see the transcript while naming speakers
- Default to k=3 on monotonic decline (over-split) rather than k=2 (under-split) — user can always re-run with explicit count

**Issues encountered:**
- None

### 2026-03-17 — Diarizer v2 fix: end_time estimation + auto-select batch source

**What was done:**
- **`diarizer.py`** (MODIFIED): Testing showed diarizer still misidentified speakers. Root cause: `load_transcript_from_markdown` sets `end_time = start_time` for all segments, so every segment got a fixed 3-second audio window regardless of actual speech length. This caused the same boundary-crossing problem as v1's sliding windows. Fix: added `_estimate_end_times()` method that sets each segment's end_time to the next segment's start_time (capped at 15s max). This gives accurate audio slices matching actual speech boundaries.
- **`ui/reviewer.py`** (MODIFIED): "Identify Speakers" now auto-selects the best source transcript. Prefers the latest batch (non-diarized) version over the original real-time transcript, since batch produces longer, more coherent segments with better acoustic features. If only the real-time version exists, shows a tip: "Re-transcribe first for better speaker detection". New `_pick_best_diarize_source()` method handles the logic.

**Decisions made:**
- End_time estimation uses next segment's start_time capped at 15s — prevents one segment from swallowing long silences
- Auto-select batch source rather than just warning — reduces friction, user doesn't need to manually switch versions
- Still proceeds with real-time transcript if no batch version exists (with a hint), rather than blocking

**Issues encountered:**
- None

### 2026-03-17 — Diarizer v3: resemblyzer neural embeddings

**What was done:**
- **`diarizer.py`** (MODIFIED): Replaced 72-dim librosa acoustic features with 256-dim resemblyzer neural speaker embeddings. The v2 algorithm used hand-crafted features (MFCCs, pitch, spectral stats) that couldn't reliably distinguish voices homogenized by conferencing codecs. Neural embeddings trained specifically for speaker verification are far more discriminative.
  - Replaced `_compute_segment_features()`: removed all librosa calls (MFCCs, delta MFCCs, pyin, spectral centroid/bandwidth/rolloff/ZCR/flatness). New implementation uses `resemblyzer.preprocess_wav()` + `VoiceEncoder.embed_utterance()` to produce 256-dim L2-normalized embeddings.
  - Added `_get_encoder()`: lazy-loads the resemblyzer VoiceEncoder on first use (CPU inference).
  - Updated `_extract_segment_features()`: removed `import librosa`, updated call to `_compute_segment_features()` (no librosa parameter).
  - Updated `_normalize_features()`: removed StandardScaler (would distort the embedding space). Resemblyzer embeddings are already L2-normalized — euclidean distance on unit vectors is monotonically related to cosine distance, so ward-linkage clustering works directly.
  - Removed `import warnings` (no longer needed without librosa pyin).
  - No changes to clustering algorithm (`_cluster_speakers`) — ward linkage + elbow detection works well with neural embeddings.
- **`requirements.txt`** (MODIFIED): Replaced `librosa>=0.10.0` with `resemblyzer>=0.1.3`.
- **`setup.bat`** (MODIFIED): Added torch CPU-only install before requirements.txt (`pip install torch --index-url https://download.pytorch.org/whl/cpu`). Added step [4/4] to pre-download the resemblyzer voice encoder model. Renumbered steps to [1/4]..[4/4].
- **Documentation**: Updated `architecture.md` (diarizer module description: neural embeddings instead of librosa features).

**Decisions made:**
- resemblyzer chosen over pyannote/speechbrain: pre-trained model auto-downloads (no HuggingFace account), tiny CPU model (~50MB), 256-dim embeddings purpose-built for speaker discrimination, simple API
- CPU-only torch: diarization model is tiny — CPU inference is fast enough. CPU-only torch is ~200MB vs ~2.5GB for CUDA torch. Diarizer runs on thread 6 (mutually exclusive with Whisper GPU) so no GPU conflict anyway.
- Removed StandardScaler normalization: resemblyzer embeddings are already L2-normalized unit vectors. StandardScaler would zero-center and scale each dimension independently, distorting the learned embedding geometry.
- Kept all other pipeline components unchanged: segment slicing, clustering, relabeling, speaker panel UI, speaker_map.json persistence

**Issues encountered:**
- None

### 2026-03-17 — Switchable diarization backends (speechbrain + pyannote + resemblyzer)

**What was done:**
- **`config.py`** (MODIFIED): Added `diarize_backend: str = "speechbrain"` and `hf_token: str = ""` fields. Backend choice persists across restarts via config.json.
- **`diarizer.py`** (MODIFIED): Added `backend` and `hf_token` fields to `DiarizeConfig`. Rewrote `_get_encoder()` to lazy-load the correct backend model: speechbrain ECAPA-TDNN (192-dim), pyannote Inference (~512-dim), or resemblyzer VoiceEncoder (256-dim). Rewrote `_compute_segment_features()` to dispatch per backend. Added three private embed methods: `_embed_speechbrain()`, `_embed_pyannote()`, `_embed_resemblyzer()`. All backends produce well-scaled embeddings — clustering pipeline unchanged.
- **`ui/reviewer.py`** (MODIFIED): Added backend dropdown (`ttk.Combobox`) in the bottom bar between "Identify Speakers" button and progress bar. Three options: "speechbrain (default)", "pyannote (HF token)", "resemblyzer (lightweight)". Pyannote validation on click: checks `import pyannote.audio` (shows install instructions on ImportError), prompts for HF token via `simpledialog.askstring` if not set. New `set_backend_config()` method for main.py to initialize dropdown from config. Updated `_on_diarize` callback signature to pass `backend` + `hf_token`.
- **`main.py`** (MODIFIED): Updated `_on_diarize()` to accept `backend` and `hf_token` parameters. Creates `DiarizeConfig(backend=backend, hf_token=hf_token)` and passes to `Diarizer`. Persists selected backend + token to app config on each diarization run. Calls `reviewer.set_backend_config()` on reviewer creation.
- **`requirements.txt`** (MODIFIED): Added `speechbrain>=1.0.0,<1.1.0` and `huggingface_hub>=0.25.0,<1.0.0` (speechbrain 1.0.3 uses deprecated `use_auth_token` removed in huggingface_hub 1.0). Kept `resemblyzer>=0.1.3`. pyannote.audio is NOT in requirements — optional install by user.
- **`setup.bat`** (MODIFIED): Renumbered to [1/5]..[5/5]. Pinned `torch==2.8.0 torchaudio==2.8.0` CPU-only (speechbrain 1.0.3 uses `torchaudio.list_audio_backends()` removed in torchaudio 2.9). Step [4/5] pre-downloads speechbrain ECAPA-TDNN model. Step [5/5] pre-downloads resemblyzer model.
- **Documentation**: Updated `architecture.md` (diarizer section lists 3 backends, config section lists new fields). Updated `development-log.md` (this entry).

**Decisions made:**
- speechbrain is the default because ECAPA-TDNN has the best EER (0.80%) and requires no HuggingFace token — model auto-downloads from public hub
- pyannote is optional (not in requirements.txt) because it requires HF account + token + accepting model license terms — too much friction for default setup
- resemblyzer kept as lightweight fallback — already bundled, no new dependencies, works for simple cases
- Backend choice persisted in config.json so user doesn't have to reselect each time
- All three backends share the same clustering pipeline (ward linkage + elbow detection) — only the embedding extraction differs
- torch/torchaudio pinned to 2.8.0 CPU-only — speechbrain 1.0.3 uses `torchaudio.list_audio_backends()` removed in 2.9, and `use_auth_token` removed in huggingface_hub 1.0
- speechbrain `savedir` uses absolute path via `PROJECT_ROOT` — `run.bat` sets cwd to `src/`, so relative paths would miss the cached model

**Issues encountered:**
- speechbrain 1.0.3 incompatible with torchaudio 2.10 (`list_audio_backends` removed) — pinned to 2.8.0
- speechbrain 1.0.3 incompatible with huggingface_hub 1.0+ (`use_auth_token` removed) — pinned to <1.0.0
- speechbrain model `savedir` used relative path — would resolve differently under `run.bat` (cwd=`src/`) vs `setup.bat` (cwd=root). Fixed to absolute path via `PROJECT_ROOT`

### 2026-03-24 — In-app pyannote installation

**What was done:**
- **`ui/reviewer.py`** (MODIFIED): When user selects pyannote backend and clicks "Identify Speakers", if `pyannote.audio` is not installed, the app now offers to install it via a yes/no dialog instead of just showing an error. Clicking "Yes" runs `pip install pyannote.audio` in a background thread with an indeterminate progress bar and "Installing pyannote.audio..." status text. All buttons are disabled during installation. On success, shows a confirmation dialog; on failure, shows the pip error output. User can then click "Identify Speakers" again to proceed.
- Added `_installing_package` flag to track install state and disable buttons.
- Added `_install_pyannote()` method (spawns subprocess in daemon thread) and `_on_install_complete()` callback (runs on main thread via `root.after()`).

**Decisions made:**
- Uses `subprocess.run([sys.executable, "-m", "pip", "install", ...])` to ensure pip runs for the correct Python environment
- 10-minute timeout on the install subprocess
- After install, user must click "Identify Speakers" again (no auto-retry) — simpler flow, avoids edge cases with freshly-imported modules
- Indeterminate progress bar since pip install doesn't report percentage

### 2026-03-25 — Fix pyannote backend + HF token management UI

**What was done:**
- **`diarizer.py`** (MODIFIED): Fixed `use_auth_token` → `token` parameter in `Model.from_pretrained()` call. Newer `pyannote.audio` (v3+) and `huggingface_hub` renamed the parameter; the old name caused silent authentication failures. Added early encoder validation — loads the encoder once before feature extraction so failures surface immediately with a clear progress error ("Encoder load failed: ...") instead of silently failing per-segment and returning no results. Added missing-token guard that raises a descriptive `RuntimeError` if pyannote backend is selected without an HF token.
- **`main.py`** (MODIFIED): Added tray notification when diarization returns no result (non-cancelled) — previously the UI just reset silently. Added `_on_hf_token_changed()` callback that persists token changes to `config.json`. Wired new `on_hf_token_changed` callback to `TranscriptReviewer`. Added file logging — `tachyon.log` in project root captures all DEBUG+ output with full timestamps, always written regardless of `python` vs `pythonw` launcher.
- **`ui/reviewer.py`** (MODIFIED): Added HF token management button in the bottom bar next to the backend dropdown. Shows masked token preview (e.g. "HF: hf_zWD...aQEF") or "HF Token" if none set. Clicking opens a modal dialog with: token entry field (masked by default with Show toggle), Save button (sets/replaces token), Delete Token button (red, clears token from memory and config.json), Cancel button. New `on_hf_token_changed` callback parameter propagates changes to `main.py` for config persistence. `set_backend_config()` now also updates the token button text.

**Decisions made:**
- Token dialog uses `show="*"` masking by default with a "Show" checkbox toggle — protects token visibility in screen shares
- Delete Token explicitly sets `hf_token` to empty string in both reviewer memory and config.json — no orphaned tokens
- Token button only visible when pyannote backend is selected — avoids confusing users on other backends
- `debug.bat` created — runs with `python` (console visible) instead of `pythonw` (headless), shows all output inline plus `pause` on exit to catch crash tracebacks

**Issues encountered:**
- `use_auth_token` deprecation was the root cause of pyannote failing silently — broad `except` in `_compute_segment_features()` swallowed the error for every segment, resulting in 0 features and a quiet `return None`

### 2026-03-25 — Fix pyannote dependency + clustering + segment merging + UI fixes

**What was done:**
- **Missing `omegaconf`**: pyannote.audio 4.0.4 requires `omegaconf` transitively (via pyannote-pipeline/lightning), but it wasn't installed. Installed `omegaconf==2.3.0`. The `torchcodec` warning is harmless — diarizer passes in-memory waveforms (`{"waveform": tensor, "sample_rate": int}`) so file-based audio decoding (which needs torchcodec/FFmpeg) is never used.
- **`diarizer.py`** (MODIFIED): Three changes:
  1. **Clustering**: Replaced broken elbow detection with max-silhouette selection. The old `_find_elbow()` used second-derivative analysis on silhouette scores — wrong method (elbow is for monotonically-decreasing metrics like inertia, silhouette should be maximized). Removed `_find_elbow()` static method and "bump k=2 to k=3" heuristic entirely.
  2. **Segment merging**: Added `_merge_consecutive_segments()` step after relabeling. When diarization assigns the same speaker to adjacent segments, they're now combined into a single block (joined text, first start_time, last end_time). This produces cleaner transcripts — e.g. 4 consecutive "Speaker 1" lines become one paragraph.
  3. Updated step numbering: merge is step 7, profiling is step 8.
- **`ui/reviewer.py`** (MODIFIED): Four changes:
  1. **Import freeze fix**: Replaced `import pyannote.audio` in `_validate_pyannote()` with `importlib.util.find_spec("pyannote.audio")`. The full import loads the entire pyannote dependency chain (torch, lightning, torchaudio, torchcodec) on the main thread, freezing the UI for ~2+ seconds on first use each session. `find_spec()` checks module existence without importing — nearly instant.
  2. **Immediate feedback**: "Identify Speakers" click now immediately switches the button to "Cancel", starts an indeterminate progress bar spinner, and shows "Starting diarization..." BEFORE the callback fires. `update_idletasks()` forces a repaint so the user sees feedback instantly. Progress bar switches from indeterminate to determinate on the first real progress callback.
  3. **Version display name mismatch**: Fixed `on_diarize_complete()` and `on_retranscribe_complete()` to use `_version_display_name_with_context()` instead of static `_version_display_name()`. The static method returns "vN (batch)" for all versioned files, but the dropdown was populated with "(diarized)" labels. The mismatch meant the dropdown didn't properly select the new diarized version.
  4. **Progress bar cleanup**: `set_diarize_running(False)` and `on_diarize_complete()` now stop the indeterminate animation and reset to determinate mode before setting values.

**Decisions made:**
- Max-silhouette is the standard method for selecting k — simply pick the k with highest score
- Segment merging runs after relabeling, before profiling — profiles reflect merged segment counts/durations
- Merging uses simple consecutive-speaker check — no time gap threshold needed since transcript segments are already properly ordered
- `find_spec()` for import validation instead of full import — avoids blocking the tkinter main thread with heavy transitive imports
- Indeterminate → determinate progress transition gives immediate visual feedback while still showing meaningful percentages once the worker reports progress

**Issues encountered:**
- pyannote.audio 4.0.4's dependency chain doesn't properly declare omegaconf — had to install manually
- `import pyannote.audio` on the main thread froze the UI for ~2s due to heavy transitive imports (torch, lightning, etc.)
- Version display name mismatch caused the reviewer to not auto-select the new diarized transcript after completion

### 2026-03-25 — Multi-loopback audio capture + device selection UI

**What was done:**
- **Multi-loopback capture** (`capture.py`): Complete rewrite of loopback handling. Instead of capturing a single system audio device, Tachyon can now capture from multiple WASAPI output devices simultaneously (e.g., Arctis 7 headset with separate Chat and Game channels). Each loopback device gets its own `_LoopbackState` with independent stream, WAV writer, buffer, and resampler. WAV naming: single loopback = `system.wav` (backward compatible), multiple = `system_0.wav`, `system_1.wav`, etc. Writes `device_manifest.json` in each session's `audio/` directory mapping filenames to device names, labels, and source tags. New `get_loopback_devices()` static method for tray UI enumeration. Source tag convention: `"you"` for mic, `"them"` for single loopback (backward compatible), `"them:Chat"`, `"them:Game"` for multi-loopback.
- **Config** (`config.py`): Added `LoopbackDevice` dataclass (`device_name`, `label`, `enabled`) and `loopback_devices: list[dict]` field to `Config`. Added `get_active_loopback_devices()` helper with auto-migration from legacy `output_device` field (if no `loopback_devices` configured but `output_device` is set, creates a single entry).
- **Transcriber** (`transcriber.py`): Replaced `_SPEAKER_LABELS` dict with `_resolve_speaker_label()` function that handles multi-loopback source tags: `"them:Chat"` -> `"Them (Chat)"`. No changes needed to overlap buffer logic (already dict-keyed by source string).
- **Tray UI** (`ui/tray.py`): Added "Loopback Devices" submenu listing all WASAPI output devices with checkbox-style `*` prefix for enabled ones. "System Default (single)" entry resets to default behavior. Added `on_set_loopback_devices` callback, `set_loopback_devices()` state method.
- **Main wiring** (`main.py`): Added `_discover_wav_files()` helper that reads `device_manifest.json` to find all WAV files in a session. Wired loopback device callbacks between tray and config. `_on_start_recording()` builds `loopback_configs` from config. Updated `_diarize_worker()` and `_on_save_speaker_names()` to use manifest-based WAV discovery instead of hardcoded filenames.
- **Exporter** (`exporter.py`): Added `_build_audio_links()` helper that reads `device_manifest.json` to generate dynamic Markdown audio links. All three export functions (`export_transcript`, `export_transcript_versioned`, `export_transcript_diarized`) now use it. Speaker legend annotates "Them (Label)" speakers with "(system audio)".
- **Batch** (`batch.py`): Added `_discover_audio_files()` method reading manifest. `transcribe_session()` loads and transcribes each loopback WAV independently with appropriate speaker label. Crosstalk suppression runs mic vs each loopback independently.
- **Diarizer** (`diarizer.py`): Added `_discover_loopback_wavs()` method. `diarize_session()` discovers loopback WAVs via manifest, merges them for embedding extraction.
- **Overlay** (`ui/overlay.py`): Replaced hardcoded `speaker_you`/`speaker_them` tags with dynamic speaker color system. New `_get_speaker_tag()` method assigns colors from a 5-color palette (`_SPEAKER_PALETTE`) on first appearance. "You" = blue, each "Them (X)" variant gets a distinct color.
- **Reviewer** (`ui/reviewer.py`): Replaced `has_system_wav: bool` with `loopback_files: list[dict]` in `SessionInfo`. `discover_sessions()` reads `device_manifest.json` for loopback file info, falls back to checking `system.wav`. Updated all button state checks to use `loopback_files` instead of `has_system_wav`.

**Backward compatibility:**
- Old sessions (no `device_manifest.json`): all code falls back to `mic.wav`/`system.wav`
- Old configs (`output_device` set, no `loopback_devices`): auto-migrated to single LoopbackDevice
- Single loopback still produces `system.wav` filename
- `speaker != "You"` filter for diarization still works (all loopback speakers are "Them" or "Them (X)")

**Decisions made:**
- Source tag convention ("them" vs "them:Label") allows the overlap buffer dict to automatically get separate buffers per loopback without code changes
- `device_manifest.json` is the inter-module communication mechanism — written by capture.py, read by batch.py, diarizer.py, exporter.py, reviewer.py
- Single loopback preserves `system.wav` naming for zero-change backward compatibility
- Dynamic speaker colors in overlay use a 5-color palette (orange, green, pink, yellow, cyan) beyond "You" blue

**Issues encountered:**
- None

### 2026-03-25 — Sliding window diarization (fix speaker grouping accuracy)

**What was done:**
- **`diarizer.py`** (MODIFIED): Replaced segment-aligned embedding extraction with a sliding window approach. The old pipeline extracted one embedding per transcript segment, which failed when segments were short (~3-5s), didn't align with speaker turns, or spanned speaker changes — producing noisy mixed embeddings that poisoned clustering. Evidence: the 2026-03-25_233618 session (51s, multiple speakers) produced 4 different wrong groupings across 3 diarization runs because results depended entirely on Whisper's arbitrary segment boundaries.
  - **New `_extract_window_features()`**: Sweeps fixed 3.0s windows with 1.5s hop (50% overlap) across the full system audio, independently of transcript segments. Skips near-silent windows (RMS < 0.01). A 51s recording produces ~32 windows (vs 9-16 segments), giving the clustering algorithm more reliable and stable data.
  - **New `_build_speaker_timeline()`**: After clustering window embeddings, builds a per-second speaker map via majority vote — for each second of audio, counts votes from all overlapping windows and assigns the speaker with the most votes.
  - **New `_relabel_from_timeline()`**: For each non-"You" transcript segment, looks up its time range in the per-second timeline and assigns the speaker covering the most seconds (majority vote). "You" segments pass through unchanged.
  - **Removed `_extract_segment_features()`**: No longer called — replaced by window-based extraction.
  - **Removed `_relabel_segments()`**: Replaced by timeline-based relabeling.
  - **Updated `diarize_session()`**: Steps 3→ `_extract_window_features()`, 5→ `_build_speaker_timeline()`, 6→ `_relabel_from_timeline()`. Steps 1, 2, 4, 7, 8 unchanged.
  - Updated module docstring to reflect new pipeline.
  - Removed unused `Sequence` import.

**Why this is better:**
- **Decoupled from transcript segments**: Window placement is based on audio timing, not Whisper's arbitrary text breaks
- **Detects mid-segment speaker changes**: Multiple windows cover each segment's time range, so a segment spanning a speaker change gets assigned to the dominant speaker
- **More data points**: More windows than segments, giving clustering more reliable data
- **Stable across runs**: Same audio → same windows → same embeddings → same clustering
- **No new dependencies**: Uses the same embedding backends and clustering algorithm

**Decisions made:**
- Window size 3.0s chosen because neural speaker embeddings need ~2-3s of speech for reliable results
- 50% overlap (1.5s hop) ensures smooth transitions and every point in audio is covered by at least 2 windows
- Energy threshold RMS < 0.01 skips silence without being too aggressive on quiet speech (audio is already RMS-normalized to 0.05)
- Per-second timeline resolution is sufficient for speaker turn detection (sub-second precision not needed for transcript segment assignment)
- Kept `_compute_segment_features()` unchanged — it's the per-window embedding function, just renamed in usage context

### 2026-03-25 — Show backend name in diarized version labels

**What was done:**
- **`exporter.py`** (MODIFIED): Added `backend` parameter to `export_transcript_diarized()`. Version header now reads e.g. `**Version**: v6 (Diarized — pyannote)` instead of just `v6 (Diarized)`. Backend defaults to empty string for backward compatibility with old transcripts.
- **`main.py`** (MODIFIED): Stashes `self._diarize_backend` from the `_on_diarize()` callback. `_diarize_worker()` passes it to `export_transcript_diarized()`. `_on_save_speaker_names()` extracts the backend name from the existing transcript header (regex on `Diarized — (\w+)`) so re-exports preserve the backend label.
- **`ui/reviewer.py`** (MODIFIED): `_version_display_name_with_context()` extracts backend name from file header and includes it in the dropdown label, e.g. `v6 (diarized, pyannote)` instead of `v6 (diarized)`.

**Decisions made:**
- Backend name uses em dash separator in markdown header: `(Diarized — pyannote)` — readable and easy to regex
- Old transcripts without backend info still display as `vN (diarized)` — backward compatible
- Re-export with speaker names preserves the original backend label from the file header

### 2026-03-25 — Fix WAV files unplayable in standard audio players

**What was done:**
- **`capture.py`** (MODIFIED): Changed WAV file subtype from `FLOAT` (32-bit float) to `PCM_16` (16-bit signed integer) for both mic and loopback writers. Windows Media Player, VLC, and most audio players cannot play 32-bit float WAVs. The internal pipeline still processes audio as float32 — `soundfile` handles the conversion on write. No impact on transcription quality (Whisper reads the files back as float32 regardless of storage format).

**Decisions made:**
- PCM_16 chosen over PCM_24 or PCM_32 for maximum compatibility — 16-bit is the universal WAV standard
- Existing sessions still have float WAVs — no migration needed since the diarizer/batch already read them fine via soundfile

### 2026-03-26 — Editable transcript with speaker reassignment

**What was done:**
- **`ui/reviewer.py`** (MODIFIED): Added full in-app transcript editing. Changes:
  1. **Edit/Save toggle button**: Green "Edit" button in the bottom bar. Clicking enters edit mode (text becomes editable, button turns red "Save"). Clicking "Save" parses the edited text back to segments and overwrites the file.
  2. **Segment marks**: `_display_segments()` now places `seg_{i}_header` and `seg_{i}_text` marks at segment boundaries. These enable precise parsing of which text belongs to which segment when saving edits.
  3. **Right-click context menu**: In edit mode, right-clicking on a segment shows "Change Speaker >" (submenu of all speakers + "New Speaker...") and "Split Segment Here" (only when cursor is in the text body, not the header).
  4. **Speaker reassignment**: `_change_segment_speaker()` updates the segment's header in-place with the new speaker name and correct color tag.
  5. **Segment splitting**: `_split_segment_at_cursor()` splits text at cursor position into two segments (same speaker + timestamp), then refreshes the display to rebuild marks.
  6. **Edit mode guards**: Switching sessions, changing versions, or closing the window while editing prompts "Discard unsaved edits?" confirmation. Retranscribe, diarize, version dropdown, and session list are disabled during editing.
  7. **State tracking**: New `_edit_mode`, `_displayed_segments`, `_displayed_version` fields for edit lifecycle management.
- **`exporter.py`** (MODIFIED): Added `save_edited_segments()` function that overwrites a transcript file preserving the original header (everything through `---`) and writing segments in standard `**[H:MM:SS] Speaker:**` format.

**Decisions made:**
- Single Text widget approach (no separate edit widget) — marks track segment boundaries, minimizing widget complexity
- Full display refresh after split — rebuilding all marks is simpler and more reliable than renaming marks inline
- Empty segments dropped on save — user can delete segment content to remove it
- `_displayed_version` tracks which file to overwrite — no version bumping on edit (edits are in-place corrections)
- Header preserved verbatim on save — duration, version label, speaker legend, audio links unchanged

**Issues encountered:**
- None

### 2026-04-12 — Share readiness hardening + deterministic test suite

**What was done:**
- **Bootstrap fixes**:
  - `setup.bat`: fixed Python launcher fallback invocation (`py -3.11`) by splitting executable/args (`PY311_EXE` + `PY311_ARGS`) so venv creation works when Python is only available through the launcher.
  - `setup.bat`: quoted pip version specifiers (`"webrtcvad-wheels>=2.0.10"`, `"resemblyzer>=0.1.3"`) to prevent CMD redirection parsing.
  - `setup.bat`: aligned torch pins to `torch==2.8.0` / `torchaudio==2.8.0` CPU to match `update.bat` and known speechbrain compatibility constraints.
  - `run.bat`: added missing-venv guard with actionable error (`Run setup.bat first`).
- **Recording stop reliability**:
  - `transcriber.py`: added drain-aware shutdown (`stop(drain=True)`) so queued tail chunks are processed before worker exit.
  - `main.py`: changed recording stop flow to call `self._transcriber.stop(drain=True)` after capture flush/stop.
- **Repo hygiene**:
  - Added root `.gitignore` to exclude local runtime artifacts and secrets (`.venv/`, `output/`, `src/output/`, `models/`, `tachyon.log`, `config.json`, caches, editor/OS noise).
- **Share-facing docs**:
  - Added root `README.md` with requirements, setup/run steps, offline/local behavior caveats, privacy notes, and limitations.
  - Updated `docs/architecture.md` wording from strict “100% local/no network” to local runtime processing with setup-time model download caveat.
  - Updated `docs/implementation-plan.md` context to reflect local-first runtime with initial internet requirement for dependency/model setup.
- **Automated tests**:
  - Added `requirements-dev.txt` with `pytest`.
  - Added deterministic test suite under `tests/`:
    - `test_config.py`
    - `test_exporter.py`
    - `test_batch.py`
    - `test_session.py`
    - `test_reviewer_discovery.py`
    - `test_transcriber_labels.py`
    - `conftest.py` (adds `src/` to import path)

**Verification:**
- `python -m pytest tests -q` → **19 passed**.
- No IDE lint errors in modified files.

**Issues encountered:**
- Host environment initially lacked `pytest`; installed via `requirements-dev.txt`.
- Running `python -m pytest -q` across the whole repo included ad-hoc root tests requiring additional heavy dependencies. Verification command narrowed to maintained suite: `python -m pytest tests -q`.

**What's next:**
- Perform a true clean-machine bootstrap/manual UI smoke run (`setup.bat` → `run.bat` → start/stop recording) on target Windows + GPU hardware before external distribution.

### 2026-04-12 — Docs cleanup sync pass

**What was done:**
- **README polish** (`README.md`):
  - Added contributor-facing references to `update.bat` and `debug.bat`.
  - Added test setup/run instructions using `requirements-dev.txt` and `pytest`.
  - Clarified that `requirements.txt` includes CUDA runtime wheels used by faster-whisper on Windows.
- **Implementation plan refresh** (`docs/implementation-plan.md`):
  - Updated architecture and Step 2 to reflect mic capture via `sounddevice` and loopback via `PyAudioWPatch`.
  - Updated project structure section to include current files/modules (`diarizer.py`, `docs/`, `requirements-dev.txt`, `update.bat`, `debug.bat`, `README.md`) and removed stale `assets/icon.png` assumption.
  - Updated tray/config sections to include current capabilities (reviewer, mic/loopback selection, diarization backend/token config).
  - Updated Step 10 launcher snippets to match current `setup.bat`/`run.bat` behavior (including venv guard and module launch).
  - Replaced stale technical notes (old sounddevice loopback claim, fixed 2-thread capture assumption) with current implementation details.
- **Internal docs alignment**:
  - `docs/architecture.md`: changed GPU constraint wording from hard-required to strongly recommended for practical real-time usage.
  - `CLAUDE.md`: updated project overview/local-first wording and refreshed project structure tree to match the current repo.
- **Repo-facing doc artifact cleanup**:
  - Deleted stale ad-hoc debug test script `test_transcribe.py` from repo root.
  - Added `.pytest_cache/` to `.gitignore` to keep root clean after test runs.
  - Kept explicit no-license-yet note in `README.md` (license decision deferred).

**Verification:**
- Re-read and cross-checked `README.md`, `docs/architecture.md`, `docs/implementation-plan.md`, and `CLAUDE.md` for consistency on:
  - loopback implementation (`PyAudioWPatch`),
  - setup/run flow,
  - file layout,
  - local/offline claims,
  - optional diarization dependencies.
- Confirmed no stale key phrases remain via targeted searches.

**Issues encountered:**
- None.

### 2026-04-29 — HF token button privacy label follow-up

**What was done:**
- Updated reviewer HF token button text in `ui/reviewer.py` to stop showing masked token fragments in the toolbar.
- New button text behavior:
  - no token saved: `HF Token`
  - token saved: `HF Saved`
- Kept existing token dialog, save/delete flow, backend gating (`pyannote` only), and config persistence unchanged.

**Decisions made:**
- Chose a binary state label instead of any partial token display to reduce accidental secret exposure and save horizontal toolbar space.

**Verification:**
- `ReadLints` for `src/tachyon/ui/reviewer.py` (no diagnostics).
- `python -m pytest tests/test_reviewer_discovery.py tests/test_diarizer.py` (pass, 5 tests).

**Issues encountered:**
- None.

## Open Issues

- None currently.

### 2026-04-13 — Diarization accuracy upgrade (JSON sidecars + multi-loopback aggregation)

**What was done:**
- **Lossless transcript sidecars** (`exporter.py`): Added JSON sidecar export for every transcript write (`transcript.json`, `transcript_vN.json`) alongside markdown. Sidecars store exact segment timing (`start`/`end` float seconds), speaker/text, schema version, source, and duration metadata.
- **Backward-compatible transcript loading** (`exporter.py`): `load_transcript_from_markdown()` now prefers JSON sidecar data when present and falls back to markdown parsing for old sessions. This preserves legacy behavior while removing timing loss on new sessions.
- **Edited transcript synchronization** (`exporter.py`): `save_edited_segments()` now updates the JSON sidecar so post-edit diarization/review operates on current structured timing data.
- **Diarizer input improvements** (`diarizer.py`): Replaced single-longest-loopback behavior with all-loopback aggregation. Embeddings are extracted from every discovered loopback WAV and clustered together.
- **Finer speaker timeline** (`diarizer.py`): Upgraded timeline resolution from 1 second to 250ms bins for segment relabeling, reducing speaker-flip errors around short turns.
- **Speaker-count hint UI** (`ui/reviewer.py`, `main.py`): Added reviewer dropdown (`Auto`, `2..8`) and wiring to pass optional `num_speakers` into `DiarizeConfig`.
- **Tests**: Added `tests/test_diarizer.py` and expanded `tests/test_exporter.py` to cover sidecar writing/loading precision and edit-sidecar sync.

**Decisions made:**
- Keep markdown as the user-facing artifact; use JSON sidecar as the diarization/review source of truth.
- Preserve function/API compatibility by keeping `load_transcript_from_markdown()` name while adding sidecar-first behavior internally.
- Keep diarization architecture lightweight (current embedding backends + sklearn clustering) while improving input fidelity and alignment precision.

**Verification:**
- `python -m pytest tests -q` → **25 passed**.
- No linter errors in modified files.

### 2026-03-24 — Fix reviewer window initial size cutting off bottom bar

**What was done:**
- **Root cause**: In `_create_window()`, the `main_pane` (PanedWindow) was packed first with `fill=BOTH, expand=True`, claiming all available vertical space. The `bottom_frame` was packed after with `side=BOTTOM`, but tkinter's pack manager had already allocated all space to `main_pane`, so the bottom bar was clipped at the initial window size — only visible after manually resizing.
- **Fix**: Moved the entire bottom bar construction (buttons, dropdown, progress bar, status label, open folder button) to pack **before** the main pane. In tkinter's pack algorithm, widgets packed first get their space reserved first. Now `bottom_frame` reserves its 40px at the bottom, then `main_pane` fills the remaining space.
- **Also**: Increased initial window height from 550px to 620px to give more breathing room.

**Changes:**
- `ui/reviewer.py`: Reordered `_create_window()` so bottom bar is constructed and packed before the main pane. Increased `_WIN_HEIGHT` from 550 to 620.

---

### 2026-03-24 — Audit & Bug Fixes

**Full codebase audit performed** — three parallel agents reviewed docs, code quality, and runtime bugs.

**Verdict:** Feature-complete (100% of plan implemented, zero TODOs/stubs). Found 4 high-priority and 7 medium-priority issues, 0 critical.

**High-priority fixes applied:**

1. **Audio queue overflow** (`capture.py`): Changed `put_nowait()` to `put(timeout=2.0)`. Previously, chunks were silently dropped when the transcriber couldn't keep up with capture (queue maxsize=100). Now blocks up to 2 s before dropping, giving the GPU time to catch up.

2. **Timestamp race condition** (`transcriber.py`): `_session_start_time` was initialized to `None` — if the worker thread processed a chunk before `set_session_start_time()` was called, the `or` fallback produced offset=0. Now initialized to `time.time()` as a safe default. Removed the `or` fallback since the field is always a float.

3. **Thread cleanup race** (`main.py`): `_on_batch_finished()` and `_on_diarize_finished()` set thread refs to `None`, but all guard clauses already use `is_alive()`. Removed `= None` assignments so mutual exclusion checks work correctly even if cleanup callback fires before thread fully exits.

4. **Private member access** (`session.py` / `main.py`): Added `Session.start_time` public property. Main.py was accessing `session._start_time` directly — now uses `session.start_time`.

**Also fixed:**
- Overlay `destroy()` TclError: `after_cancel()` now wrapped in its own try/except so it can't prevent `quit()`/`destroy()` from running.
- Removed unused `shutil` import from `main.py`.

**Medium-priority items deferred (documented, not blocking):**
- File dialog on tray thread (tray.py:216) — should schedule on main thread
- Batch crashes if model is None — needs guard before passing to BatchTranscriber
- Thread join timeout 5 s may be too short for long Whisper ops
- Missing ImportError handling in diarizer backend loading
- Overlay `_all_segments` list grows unbounded in long sessions
- File descriptor leak in capture.py error path (WAV opened, loopback stream fails)
- Division by zero potential in batch.py with silent audio

### 2026-04-14 — Fix empty transcript, batch crash, diarize failures, and UI errors

**What was done:**

**Bug 1 — Batch re-transcription crash on empty loopback WAV:**
- **Root cause**: One loopback WAV file had 0 samples (`Loaded system audio: 0.0s, 0 samples`). After resampling, the array was zero-size. The logging line `np.max(np.abs(lb_audio))` crashed with `ValueError: zero-size array to reduction operation maximum which has no identity`.
- **Fix** (`batch.py`): Added `lb_audio.size == 0` guard after resampling — skips the empty file with a warning instead of crashing. The other loopback WAV (with real audio) is still processed normally.

**Bug 2 — Transcript shows empty after edit save:**
- **Root cause**: The user entered edit mode and saved, but the text widget mark-based parsing (`_save_edits`) produced 0 segments (likely the marks were invalidated or the text was corrupted during editing). `save_edited_segments()` wrote a sidecar with 0 segments. All subsequent loads found this sidecar, accepted it as authoritative, and displayed nothing. Log evidence: `Saved edited transcript: 0 segments to transcript.md`.
- **Fix** (`ui/reviewer.py`): `_save_edits()` now checks `if not updated_segments:` and shows a warning dialog ("Cannot save — all segments are empty") instead of writing a poisoned sidecar. Combined with the earlier sidecar fallback fix in `exporter.py` (empty sidecar falls back to markdown parsing).

**Bug 3 — pyannote validation crashes with ModuleNotFoundError:**
- **Root cause**: `importlib.util.find_spec("pyannote.audio")` tries to import the parent package `pyannote` first. When pyannote isn't installed at all, this raises `ModuleNotFoundError` instead of returning `None`. The exception was unhandled, crashing the tkinter callback.
- **Fix** (`ui/reviewer.py`): Wrapped `find_spec()` in a try/except catching `ModuleNotFoundError` and `ValueError`, treating either as "not installed".

**Bug 4 — speechbrain encoder fails: `torchaudio.list_audio_backends` removed:**
- **Root cause**: speechbrain 1.0.x calls `torchaudio.list_audio_backends()` during import/setup, but this function was removed in torchaudio >=2.9. The diarizer caught the error but returned `None` (no result), giving the user no useful feedback.
- **Fix** (`diarizer.py`): Added a compatibility shim in `_get_encoder()` — if `torchaudio.list_audio_backends` doesn't exist, patches it as `lambda: ["soundfile"]` before importing speechbrain.

**Bug 5 — Silent failure feedback (from earlier in this session):**
- **`exporter.py`**: Empty sidecar now falls back to markdown parsing.
- **`main.py`**: Batch and diarize failure paths now call `reviewer.show_error()` with descriptive messages.
- **`ui/reviewer.py`**: Added `show_error()` public method for in-app error display.

**Root cause chain for the user's issue:**
1. Recording produced 1242 segments successfully (real-time transcription worked fine)
2. One of two loopback devices produced an empty WAV (0 samples) — possibly a virtual/inactive audio device
3. User entered edit mode → save parsed 0 segments → wrote empty sidecar → transcript appeared blank
4. Re-transcribe hit `np.max()` on the empty loopback array → crashed
5. Diarize (speechbrain) failed on `torchaudio.list_audio_backends` → returned no result silently
6. Diarize (pyannote validation) crashed on `find_spec` → tkinter exception

**Decisions made:**
- Empty loopback files are skipped with a warning rather than aborting the entire batch — the mic and other loopback files still have valid audio
- Zero-segment edit saves are blocked at the UI level with a warning dialog — never let a poisoned sidecar reach disk
- `torchaudio.list_audio_backends` shim returns `["soundfile"]` — minimal compatibility fix that lets speechbrain proceed without requiring a torchaudio downgrade
- `find_spec` wrapped in try/except — handles the edge case where the parent namespace package doesn't exist

**Issues encountered:**
- `torchaudio` version pinning in requirements.txt (`torch==2.8.0 torchaudio==2.8.0`) may have been overridden by a later `pip install` — the running version appears to be >=2.9 where `list_audio_backends` was removed

### 2026-04-14 — UI overhaul: theme system, reviewer restructuring, overlay polish, tray refinements

**What was done:**

- **`ui/theme.py`** (NEW): Centralised visual theme with semantic colour tokens (`Color`), font definitions (`Font`), layout dimensions (`Dim`), overlay speaker palette, and `ToolTip` class for hover tooltips. All UI files now import from theme instead of scattering hex codes.
- **`ui/reviewer.py`** (REWRITTEN): Major UI overhaul:
  - **Window management**: Resizable with `minsize(900, 650)`, saves/restores geometry to config via `on_save_geometry` callback and `set_initial_geometry()` method
  - **Top toolbar**: Replaced cramped 40px bottom bar with 44px top toolbar with three logical groups separated by vertical separators — Actions (Re-transcribe, Identify Speakers, Edit), Status (progress bar + label), Config (backend dropdown, speaker count, HF token, Open Folder)
  - **Session list enrichment**: Replaced plain `tk.Listbox` with custom `tk.Canvas`-based scrollable list of rich entries. Each session shows full date ("Apr 14, 1:01 PM"), duration + version count on a second line, and a green dot for diarized sessions. Data sourced from JSON sidecars (duration) and `speaker_map.json` existence (diarization status). Added `SessionInfo.duration_sec` and `SessionInfo.is_diarized` fields.
  - **Search/filter**: Added search entry at top of session list panel with placeholder text, filters sessions by date substring match
  - **Transcript header**: Larger 14pt bold session title with full date/time, "Edit Speakers" is now a proper styled button
  - **Typography**: Body text bumped to 12pt (from 11), `spacing3=6` on text widget for segment separation, coloured left border character (`┃`) before each segment for visual scanning
  - **Colours**: Transcript background `#222222` (lighter than sidebar `#252526`), 1px `#3e3e3e` borders between sections, consistent accent blue for active states
  - **Keyboard nav**: `Ctrl+E` toggles edit mode, `Ctrl+R` triggers re-transcribe, `Escape` exits edit mode (with unsaved changes confirmation)
  - **Tooltips**: All toolbar buttons have hover tooltips explaining their function
- **`ui/overlay.py`** (REWRITTEN): Theme migration plus:
  - **Border**: 1px `#333333` border via `highlightbackground` on main frame
  - **Recording indicator**: Pulsing red dot (`●`) in titlebar when `set_recording(True)` is called, toggles visibility every 800ms
  - **Segment dividers**: Horizontal line (`─` × 40) between different speakers in expanded mode
  - **Fade transitions**: `_fade_to()` method smoothly transitions alpha over ~200ms using `root.after()` in 20ms increments
  - **Cleanup**: Cancels all `after()` callbacks (poll, pulse, fade) on destroy
- **`ui/tray.py`** (REWRITTEN): Theme migration plus:
  - **Better icon**: Gradient circle (darker outer ring, lighter center), bolder "T" glyph at 36pt, small yellow lightning bolt accent for "tachyon = speed" motif
  - **Recording state**: Entire icon switches to red-tinted background instead of overlaying a small dot — much more visible
  - **Last session info**: "Last: Apr 14, 1:01 PM" disabled menu item at top of context menu, updated via `set_last_session_time()` method
- **`config.py`** (MODIFIED): Added `reviewer_geometry: Optional[str]` and `overlay_expanded_size: Optional[tuple[int, int]]` fields with JSON serialization support
- **`main.py`** (MODIFIED): Wired new callbacks — `on_save_geometry` for reviewer window persistence, `overlay.set_recording()` for pulsing dot, `tray.set_last_session_time()` after export, `reviewer.set_initial_geometry()` from config

**Decisions made:**
- Theme uses class-based attribute containers (not dicts) for IDE autocomplete and static analysis
- Session list uses Canvas+Frame pattern instead of Listbox — enables multi-line entries, icons, and custom selection styling
- Toolbar uses `ttk.Separator` for visual group dividers — crisp 1px lines that match the dark theme
- Overlay border uses `highlightbackground` on the frame rather than a separate border widget — simpler and no layout impact
- Lightning bolt on tray icon is drawn with Pillow `draw.line()` — 4-point polyline at 2px width

**Issues encountered:**
- None — all 25 tests pass

## Deviations from Plan

- **Tray icon**: Generated programmatically instead of using `assets/icon.png`. The `assets/` directory exists but is unused. This is simpler and avoids an external dependency.
- **Exporter typing**: Uses `Protocol` types (`SessionLike`, `TranscriptSegmentLike`) instead of importing concrete classes. This provides loose coupling at the cost of slightly more code in `exporter.py`.
- **Loopback capture**: Uses `PyAudioWPatch` instead of `sounddevice` for WASAPI loopback. `sounddevice` does not support loopback in any version — its bundled PortAudio DLL lacks the feature.
- **CUDA runtime**: Requires `nvidia-cublas-cu12` and `nvidia-cudnn-cu12` pip packages + `os.add_dll_directory()` registration in `main.py`, since CTranslate2 doesn't bundle these on Windows.
- **Speaker diarization**: Three switchable backends — speechbrain ECAPA-TDNN (default, 192-dim), pyannote (optional, ~512-dim, requires HF token), resemblyzer (fallback, 256-dim). All use sklearn clustering. Originally used resemblyzer only, then expanded to three backends for better accuracy on codec-compressed audio. pyannote is optional (not in requirements.txt).
- **Multi-loopback audio**: Original plan used binary "You"/"Them" model with single system audio device. Expanded to support multiple WASAPI output devices simultaneously with `device_manifest.json` inter-module communication. Source tag convention: `"them"` (single, backward compatible), `"them:Label"` (multi-loopback).

### 2026-04-14 — UI Overhaul v2: Sci-Fi Theme

**What was done:**
- **Theme overhaul** (`ui/theme.py`): Replaced flat gray palette with deep blue-teal sci-fi HUD aesthetic. All backgrounds now use blue-tinted darks (#0a0e17 through #151d2e). Accent changed from muted blue to bright cyan (#00b4d8). Added glow tokens, card state tokens, divider color. Added monospace font family (Cascadia Code, fallback Consolas) for timestamps. Bumped body text to 13pt. Increased toolbar height to 52px and sidebar width to 280px. Added card spacing dimensions.
- **Custom widget library** (`ui/widgets.py`, new file): Created reusable premium widgets:
  - `HoverButton`: Frame-based button with smooth 3-step animated hover transitions
  - `GlowFrame`: Canvas wrapper with semi-transparent glow border layers
  - `GradientBar`: Canvas with vertical gradient background for toolbars/headers
  - `PulseIndicator`: Canvas with animated pulsing glow circle for recording indicator
  - `SessionCard`: Rich card widget with left accent bar, hover/select states, two-line layout
  - Utility functions: `_hex_to_rgb`, `_rgb_to_hex`, `_lerp_color` for colour interpolation
- **Reviewer window** (`ui/reviewer.py`):
  - Toolbar: Wrapped in `GradientBar`, added Unicode icons to action buttons (↻ Re-transcribe, 🗣 Speakers, ✎ Edit), used `HoverButton` for Open Folder, styled separators as thin frames instead of ttk, increased padding
  - Sidebar: Sessions header gets cyan accent underline, search entry gets focus glow effect (border highlights on focus), session rows replaced with `SessionCard` widgets with hover transitions and left accent bars, improved date format ("Mar 16 — 1:58 PM")
  - Header: Two-line layout — large date on line 1, time + duration + version count on line 2. "Edit Speakers" uses `HoverButton`. Version dropdown styled dark.
  - Transcript: Increased padding (24px/16px), timestamps in monospace font, increased segment spacing (spacing3=10)
  - Speaker panel: Save/Close buttons replaced with `HoverButton`
  - Status bar: New 28px bar at bottom with session count (left) and keyboard shortcut hints (right)
  - Window icon: Set from Pillow-generated app icon via `iconphoto()`
  - Global ttk styling: Dark-themed comboboxes, scrollbars, and progress bars
- **Overlay** (`ui/overlay.py`):
  - Added 2px bright cyan "HUD edge" line at top of main frame
  - Main frame border colour changed to glow-secondary for subtle glow effect
  - Recording indicator replaced with `PulseIndicator` widget (smooth animated glow instead of crude text-swap blink)
  - Title text changed to "TACHYON" (caps), text colours updated to use theme tokens
  - Button hover colours updated to match new palette
  - Caption font bumped to 15pt for readability
  - Text widget selection colour updated
- **Tray icon** (`ui/tray.py`):
  - Extracted `create_app_icon()` as module-level function for reuse (reviewer window icon)
  - Default: Navy circle (#0a0e17) with subtle cyan outer ring, "T" in Segoe UI, bright cyan lightning bolt
  - Recording: Deep red circle with orange-red lightning bolt and red outer glow ring
  - Renders at 128x128 then downscales to 64x64 with LANCZOS for anti-aliased edges
  - Font changed from Arial to Segoe UI for consistency

**Decisions made:**
- Kept `tk.Button` for toolbar action buttons (Re-transcribe, Identify Speakers, Edit) because they have complex state management (`_update_button_state()` calls `.configure()` extensively). Used `HoverButton` only for simpler secondary buttons.
- `PulseIndicator` uses smooth RGB interpolation via `after()` loop instead of the old binary text-swap approach — much more professional-looking recording indicator.
- `GradientBar` draws in vertical bands of 4px for performance — barely visible banding at toolbar heights.
- `SessionCard` manages its own hover/select state internally — `_update_session_selection()` simply calls `card.set_selected(bool)`.
- Tray icon `create_app_icon()` extracted as module-level function so reviewer can import it without instantiating TrayIcon.

**Issues encountered:**
- None

### 2026-04-22 — Ported v1 shareable release work from pre-migration tree

**What was done:**
This work was originally produced in the pre-git-migration project tree (`X:\git\TachyonTranscripts\`) but never made it into the `projectTT` git repo. Ported it forward on top of the current HEAD, merging carefully with the post-migration changes (UI overhaul, speaker-ID updates, graceful mic/capture handling, reviewer geometry persistence).

Five merged capabilities (Tasks #1–#5 of the shareable-release plan):

1. **Hardware-aware transcriber (#1)**. New `src/tachyon/hardware.py` detects NVIDIA via NVML → `torch.cuda` → CPU fallback. `recommend_model_size()` policy: ≥10 GB VRAM → `large-v3`, ≥6 GB → `medium`, <6 GB → `small`, CPU → `distil-large-v3`. `resolve_transcriber_config()` expands `"auto"` values to concrete `(device, model, compute_type, hw)` tuples. `Transcriber.load_model()` rewritten to try the resolved config first and fall back to CPU with `int8` on CUDA failure; exposes `device`, `resolved_model_size`, `compute_type`, `fell_back_to_cpu` properties. `Config` grew `compute_device: "auto"` and `model_size` default changed from `"large-v3"` to `"auto"` (existing configs with explicit sizes are unaffected). `setup.bat` now invokes `scripts/download_model.py` instead of hard-coding `WhisperModel("large-v3", device="cuda", ...)`, so setup succeeds on non-NVIDIA machines.

2. **First-run wizard + consent gate (#2)**. New `src/tachyon/ui/wizard.py` — 5-page modal `Toplevel`: welcome + detected hardware, recording-law disclaimer with required checkbox, mic picker, loopback picker, done. `Config` grew `first_run_complete` and `consent_acknowledged` flags. `main.run()` restructured so model load + tray start happen inside the tkinter mainloop via `root.after(0, ...)`, letting the wizard render modally before the rest of startup. New `_run_wizard_then_startup` / `_post_wizard_startup` / `_show_wizard` / `_on_show_wizard` / `_start_tray_and_hotkey` methods. `_on_start_recording()` has a hard gate: if `consent_acknowledged` is False, recording is blocked with a tray notification and the wizard re-opens. `TrayIcon` accepts an optional `on_show_wizard` callback and conditionally renders a "Setup Wizard" menu item.

3. **Deferred stability fixes (#3)**. The four medium-priority items from the 2026-03-24 audit that weren't in the projectTT tree:
   - **Batch-None guard** in `_on_retranscribe`: if `self._transcriber` or `self._transcriber.model` is None, notify the user and reset the reviewer spinner instead of crashing the daemon thread inside `BatchTranscriber(model=None, ...)`.
   - **Tray-thread file dialog**: `TrayIcon.on_set_output_folder` signature changed from `Callable[[Path], None]` to `Callable[[], None]` — tray just signals intent. `App._on_set_output_folder()` schedules `_pick_output_folder` via `root.after(0, ...)`, and the actual `filedialog.askdirectory` runs on the existing tkinter main thread with `parent=self._overlay._root`. Removed `tkinter` / `filedialog` / `Path` imports from `tray.py`.
   - **FD leak in capture.py loopback path**: the existing `except` block closed `state.wav_writer` on partial failure but left `state.stream` open. Now closes the PyAudio stream handle first (so the device isn't held), then the WAV. The mic-path cleanup was already present in projectTT.
   - **Division-by-zero in batch.py `_rms` / `_normalize_rms`**: `_rms` now returns `0.0` for non-finite results (`NaN`/`Inf` from empty-slice `np.mean`). `_normalize_rms` converted to `@classmethod` and routes through `_rms`; explicit `audio.size == 0` early-return before touching RMS.

4. **Inno Setup installer (#4)**. New `installer/` directory: `tachyon.spec` (PyInstaller — `collect_all` on faster-whisper / ctranslate2, `collect_dynamic_libs` on `nvidia.cublas` / `nvidia.cudnn` / `nvidia.cuda_runtime`, UPX disabled because it breaks CUDA DLL loading), `Tachyon.iss` (per-user install at `%LocalAppData%\Programs\Tachyon Transcripts`, `AppId` GUID fixed, LZMA2/max, `InfoBeforeFile=pre_install_notice.txt` for legal disclaimer, optional desktop shortcut + start-on-login, post-install `--download-model` Run step, uninstall preserves user recordings), `build_installer.bat` (5-phase build: ensure PyInstaller, regenerate icon, clean stale `build/dist`, run PyInstaller, run Inno Setup — searches PATH then standard Program Files path for `iscc.exe`), `pre_install_notice.txt` (legal disclaimer mirroring the wizard text), `installer/README.md` (developer-facing build notes + smoke-test checklist). New `scripts/make_icon.py` generates a multi-resolution `assets/icon.ico` (16/32/48/64/128/256 px) from the same visual design as the programmatic tray icon. New `scripts/download_model.py` pre-downloads the appropriate Whisper model at setup time. `main.main()` now handles `--version` (prints `Tachyon Transcripts 0.1.0` and exits, kept in sync with `Tachyon.iss`'s `MyAppVersion`) and `--download-model` (exits 0/1/2 via `_download_model_cli()` helper so the installer's `[Run]` step can surface failures).

5. **Public release materials (#5)**. `README.md` rewritten as a hero doc for public consumption (features, install paths, SmartScreen warning, quick start, output layout, config table, troubleshooting, legal summary, MIT licence, acknowledgments). New `LICENSE` (MIT, Erik as copyright), `CONTRIBUTING.md` (dev setup, doc-sync requirement, PR checklist), `CHANGELOG.md` (Keep-a-Changelog format, `[0.1.0]` entry covering all five tasks + `[Unreleased]` placeholder), `docs/LEGAL.md` (full-length legal notice — US federal + 11 all-party states, EU/UK GDPR, Canada PIPEDA, Australia state-by-state, workplace + meeting-platform + privileged-relationship considerations, ~1300 words). `.github/ISSUE_TEMPLATE/bug_report.md` + `feature_request.md` + `config.yml` with `blank_issues_enabled: false` and contact links to legal / troubleshooting docs.

**Merge conflicts resolved (intent-not-literal):**
- `transcriber.py` — preserved the `drain` parameter on `stop()` / `_drain_on_stop` added post-migration.
- `main.py` — preserved the capture-start try/except, `overlay.set_recording()` calls, `set_last_session_time()`, `reviewer_geometry` wiring, `num_speakers` in `DiarizeConfig`, and `reviewer.show_error()` paths in batch/diarize failure handling.
- `config.py` — preserved `reviewer_geometry` and `overlay_expanded_size` fields and their tuple/list JSON round-tripping. Deliberately did NOT port the pre-migration `output_device` → `loopback_devices` auto-migration in `get_active_loopback_devices()` — projectTT intentionally dropped it because the migration re-fired every load and resurrected stale device names after Windows renames (see `tests/test_config.py::test_get_active_loopback_devices_ignores_legacy_output_device`). The legacy `output_device` field still exists for backward compatibility but is ignored at resolution time; the default-loopback fallback lives in `capture._resolve_loopback_targets` where it belongs.
- `tray.py` — preserved the post-migration sci-fi icon design (lightning bolt, recording gradient, `Dim.icon_size`), `set_last_session_time()` / `_last_session_time` and the "Last: …" menu item, and the per-session menu ordering; only added `on_show_wizard` plumbing + removed the on-thread `tk.Tk()` dialog.
- `capture.py` — kept the post-migration optional-mic path and graceful no-sources RuntimeError; only augmented the loopback failure cleanup to close `state.stream` before `state.wav_writer`.
- `batch.py` — only hardened `_rms` / `_normalize_rms`, left crosstalk/dedup logic alone.
- `setup.bat` — kept the post-migration `PY311_EXE`/`PY311_ARGS` split, quoted-version requirement specs, and torch 2.8.0 pin; only swapped step [4/6] to `scripts/download_model.py`.

**Decisions made:**
- Ported the old work forward as a single commit on top of current HEAD rather than cherry-picking five separate commits — the old tree isn't a git ancestor so cherry-pick was impossible, and a single "this work landed today" entry is more honest than back-dating five commits to 2026-04-20.
- README updated the GitHub URL to `PyroDS/projectTT`, the installer exe filename (`TachyonTranscripts-Setup-<version>.exe` matches `Tachyon.iss`'s `OutputBaseFilename`), and the output-dir description (`output/` next to the app, matching current behaviour, not `Documents\Tachyon Transcripts\`).
- Not ported: the pre-migration development-log task entries dated 2026-04-20. They describe the same work and would conflict with this summary.

**Issues encountered:**
- During the port I also discovered the venv in `X:\git\projectTT\.venv` was a rename of `X:\git\TachyonTranscripts\.venv` — the `pip.exe` shim still has the old absolute python path baked in, so `pip install <pkg>` silently installs into the ghost old venv. Workaround: use `.venv\Scripts\python -m pip install ...` instead of `pip.exe` directly. Longer-term fix: delete `.venv` and re-run `setup.bat`. Filed as an open issue below.
- Pyannote backend needed `omegaconf` installed manually (`.venv\Scripts\python -m pip install omegaconf`); it's declared as a dep by `pyannote.audio` 4.0.4 but was missing on this install. Not strictly an installer-era issue, but surfaced while verifying the port.

**What's next:**
- Task #6: smoke-test the installer. Requires a Windows machine with Inno Setup 6 + PyInstaller to run `installer\build_installer.bat`, then copying `dist\TachyonTranscripts-Setup-0.1.0.exe` to a clean Windows 10/11 VM and walking the smoke-test checklist in `installer/README.md`. Belongs to Erik (hardware wall).
- Consider re-creating `.venv` to fix the baked-in path issue in the `Scripts\*.exe` shims.

### 2026-04-21 — Graceful handling of missing mic & failed-start cleanup

**What was done:**
- **Root cause**: If the configured mic device was disconnected, `capture._resolve_device()` raised `ValueError` inside `capture.start()`. The exception propagated uncaught through `main._on_start_recording()` (no try/except) all the way up to pystray's message handler, crashing the recording start. Side effect: `self._recording` had already been set to `True` before `capture.start()` ran, leaving the UI stuck in a fake "recording" state with no actual capture. Loopback streams never got a chance to open, which is why the user also saw EDIFIER audio not being captured — recording never started.
- **Fix**: Made the microphone optional, symmetric with how loopback already degrades gracefully. Loopback-only capture is a valid mode.
- **Changes**:
  - `capture.py`:
    - `_resolve_device()` now returns `Optional[int]` instead of raising. Missing-device and no-suitable-default branches log a warning with the available device list and return `None`.
    - `start()` treats mic failure the same as loopback failure — logs a warning, continues with loopback only, exposes state via a new `mic_active` property. Only aborts (new `RuntimeError`) when neither mic nor any loopback is available.
    - `_write_device_manifest()` now takes `Optional[str]` for the mic name and omits the `"mic"` key entirely when there's no mic — keeps `data.get("mic", {}).get("file")` calls safe in batch/diarizer/exporter/main.
    - `_resolve_loopback_targets()` now logs the list of available PyAudioWPatch loopback device names when a config entry doesn't match, to make future mismatches easier to diagnose.
  - `main.py`:
    - `_on_start_recording()` wraps `capture.start()` in try/except. On failure: tears down the partial capture, resets session state, reverts tray/overlay state, notifies the user via tray, and returns without starting the transcriber.
    - `self._recording = True` / `set_recording(True)` / `overlay.set_recording(True)` moved to *after* `capture.start()` succeeds.
    - Added a user notification ("Microphone unavailable — recording system audio only.") when `capture.mic_active` is False after a successful start.
  - `exporter.py`:
    - The mic-link builder no longer defaults to `mic.wav` when the manifest omits the mic entry — it skips the link entirely so exports from no-mic sessions don't contain dead links.

**Decisions made:**
- Mic and loopback are now symmetric: both are optional, both log warnings on failure, and `start()` only raises if *every* source failed. This matches the spirit of "graceful degradation" already called out in `CLAUDE.md`.
- Omit the `"mic"` key from `device_manifest.json` in no-mic sessions (rather than `"mic": null`) so existing `data.get("mic", {}).get("file")` consumers work unchanged.
- State mutation in `_on_start_recording()` now happens strictly after the capture succeeds — the previous ordering created a window where a crash left the app visibly "recording" with no capture thread.

**Issues encountered:**
- None. The `[Loopback]` suffix handling in `_find_loopback_device()` was verified correct (substring match + consistent PyAudioWPatch naming) and did not need changes.

### 2026-04-21 — Fix "defaults" path: stale output_device migration + Windows `2-` ordinal mismatch

**What was done:**
- **Root cause (follow-up from the graceful-start fix)**: Even with mic-optional capture in place, starting a recording with `loopback_devices=[]` (i.e. user's "System Default" selection) still hit `RuntimeError: No audio sources available`. Two interacting bugs:
  1. `config.get_active_loopback_devices()` re-fired its "migration" path on every load — if `loopback_devices` was empty but the legacy `output_device` field was set, it synthesized a phantom `LoopbackDevice` from `output_device`. So "empty = default" never actually happened; instead we silently used the stale `output_device` name every time.
  2. The resurrected `output_device='Headset Earphone (Arctis 7 Chat)'` no longer matched because Windows had renamed the device to `Headset Earphone (2- Arctis 7 Chat)` after reconnect (the `2-` is an ordinal prefix for the second instance). Substring match failed, no loopback resolved, and since the mic was also offline, `start()` raised.
- **Fix**:
  - `config.py`: Removed the re-firing migration from `get_active_loopback_devices()`. An empty `loopback_devices` list now truly means "use the WASAPI default output" and is resolved downstream. The legacy `output_device` field is retained on the dataclass for backward-compat JSON loading but no longer auto-promotes into loopback config.
  - `capture.py`: `_resolve_loopback_targets()` now has a safety net — when *every* explicit `LoopbackDevice` config fails to resolve (e.g. disconnected device, or Windows ordinal-prefix rename), it falls back to `_find_default_loopback()` and logs a warning. This matches user intent ("I want something captured") without silently overriding explicit, resolvable selections.
  - `tests/test_config.py`: Replaced `test_get_active_loopback_devices_migrates_output_device` (which asserted the now-removed behavior) with `test_get_active_loopback_devices_ignores_legacy_output_device` + a `test_get_active_loopback_devices_returns_configured_entries` test for the enabled-flag filter.

**Decisions made:**
- Keep `output_device` on the dataclass so existing `config.json` files still load cleanly, but stop reading it in `get_active_loopback_devices()`. Migration semantics moved out of the hot path — the field is now effectively dead weight, to be removed in a future cleanup.
- Fall back to default loopback only when the `loopback_configs` path produces *zero* targets. If some configured devices resolve and others don't, respect the user's explicit selection and skip the missing ones (existing behavior).

**Issues encountered:**
- None. Windows' `2-` ordinal-prefix rename is the kind of thing substring-matching can't reliably catch without being overly lenient; the default-loopback fallback is the pragmatic escape hatch.

### 2026-04-29 — Reviewer toolbar backend selector clipping fix

**What was done:**
- Fixed a reviewer-toolbar layout regression where the diarization backend selector could be pushed out of view at normal window sizes (especially when the pyannote HF token button was visible).
- Updated `ui/reviewer.py` toolbar config controls to reduce horizontal pressure:
  - backend dropdown now uses compact values (`speechbrain`, `pyannote`, `resemblyzer`) instead of long decorated labels,
  - backend dropdown width reduced and paired with an explicit `Backend:` label,
  - pack order adjusted so the backend control remains visible/accessible while optional controls occupy lower priority space.
- Kept existing behavior intact for:
  - `Identify Speakers` backend resolution,
  - pyannote HF token button show/hide logic,
  - control disabling during active work / edit mode.

**Decisions made:**
- Used canonical backend keys directly in the combobox to avoid fragile parsing from display labels (`split()[0]`) and to keep state handling simple.
- Prioritized visibility of core diarization controls over secondary toolbar actions when horizontal space is constrained.

**Verification:**
- `python -m pytest tests/test_reviewer_discovery.py tests/test_diarizer.py` (pass, 5 tests).
- `ReadLints` for `src/tachyon/ui/reviewer.py` (no diagnostics).

**Issues encountered:**
- None.

### 2026-04-29 — Windows taskbar icon identity fix

**What was done:**
- Fixed the app falling back to the default Python icon in the Windows taskbar.
- Added an explicit Windows AppUserModelID during startup before the first tkinter window is created, so Windows groups the process under Tachyon instead of `python.exe`.
- Set the hidden tkinter root window's icon from the existing programmatic Tachyon icon and marked it as the default icon for child dialogs.

**Decisions made:**
- Reused `ui.tray.create_app_icon()` instead of introducing a separate icon asset for runtime windows, keeping the taskbar/window icon visually aligned with the tray icon.
- Kept the AppUserModelID stable and app-scoped (`PyroDS.TachyonTranscripts`) rather than versioned, so Windows taskbar grouping remains consistent across releases.

**Verification:**
- `ReadLints` for `src/tachyon/main.py` and `src/tachyon/ui/overlay.py` (no diagnostics).

**Issues encountered:**
- None.
