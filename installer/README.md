# Tachyon Transcripts — Installer Build Notes

This folder contains the Windows installer build pipeline:

| File | Purpose |
|---|---|
| `tachyon.spec`          | PyInstaller spec that produces `dist\TachyonTranscripts\`. |
| `Tachyon.iss`           | Inno Setup 6 script that wraps the PyInstaller output in a signed-ready `.exe` installer. |
| `build_installer.bat`   | One-shot script that runs PyInstaller + Inno Setup end-to-end. |
| `pre_install_notice.txt`| Legal disclaimer shown on the InfoBefore page of the installer wizard. |
| `dist/`                 | (Generated) output directory — the final `TachyonTranscripts-Setup-<ver>.exe` lands here. |

## Prerequisites

1. A clean Tachyon Transcripts checkout with `setup.bat` already run — this ensures `.venv\` has all runtime dependencies.
2. PyInstaller 6.x in the venv. The build script installs it automatically if missing, or you can pre-install: `.venv\Scripts\pip install "pyinstaller>=6.0"`.
3. [Inno Setup 6](https://jrsoftware.org/isdl.php) installed. The build script looks for `iscc` on PATH and at the default `%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe` location.

## Building

From the project root:

```
installer\build_installer.bat
```

The script does five things:

1. Installs PyInstaller into the venv if it's missing.
2. Generates `assets\icon.ico` from `scripts\make_icon.py` (same visual design as the tray icon, packed as a multi-resolution ICO).
3. Wipes any stale `build\` and `dist\` output.
4. Runs PyInstaller against `tachyon.spec` to produce `dist\TachyonTranscripts\TachyonTranscripts.exe` plus its supporting DLL / data bundle (~500 MB–1.2 GB depending on CUDA DLLs present).
5. Runs Inno Setup against `Tachyon.iss`, which compresses that folder into a single `installer\dist\TachyonTranscripts-Setup-0.1.0.exe`.

Total build time on a warm machine: 3–8 minutes.

## What gets bundled

### Code + runtime
- All of `src\tachyon\` (including submodules discovered via `collect_submodules("tachyon")`).
- The embeddable Python interpreter, Tcl/Tk, and standard library — supplied by PyInstaller.
- All declared `requirements.txt` dependencies plus `resemblyzer`, `webrtcvad-wheels`, `torch (CPU)`, and `speechbrain`.

### Dynamic libraries
- `ctranslate2` + `faster_whisper` binaries via `collect_all`.
- CUDA runtime DLLs (`cublas64_12.dll`, `cudnn64_9.dll`, and friends) from the `nvidia-cublas-cu12` / `nvidia-cudnn-cu12` pip packages. These are required for GPU inference; without them CTranslate2 falls back to CPU at first use rather than at import.
- PortAudio variants shipped inside `sounddevice` and `PyAudioWPatch`.

### Models
- **NOT bundled** by default — the installer is already large enough and shipping a 1–3 GB model would make it unwieldy.
- The installer offers an optional post-install `[Run]` step that calls `TachyonTranscripts.exe --download-model`. This pre-caches the recommended Whisper model into `%USERPROFILE%\.cache\huggingface\`, so the first launch doesn't block on a long download.
- If the user declines, the model downloads on first launch via `faster_whisper`'s default behaviour. The tray stays available and shows model-load status text, but the wizard still has no dedicated progress bar.

## Install behaviour

- **Per-user install**: `%LocalAppData%\Programs\Tachyon Transcripts\`. No admin elevation required, which matters because the installer is unsigned — forcing UAC would trigger a second SmartScreen warning.
- **Shortcuts**: Start Menu entry by default; desktop shortcut optional; autostart-on-login optional (unchecked by default).
- **Uninstall**: Standard "Add or Remove Programs" entry. Removes the install tree and cached model weights under `{app}\models\`, but deliberately **does not** touch user recordings (default output is `output/` next to the app unless changed in settings).

## Known rough edges (v1)

- **Installer is not code-signed.** First-time users will see a SmartScreen "Windows protected your PC" warning. Clicking "More info" → "Run anyway" lets them through. Addressing this requires an EV code-signing certificate (~$300/year) or reputation-based whitelisting, neither of which is in v1 scope. Document the warning clearly in the public README.
- **Antivirus heuristics** flag PyInstaller-built executables with some frequency because the bootloader pattern is also used by malware. If multiple users report this, the fix is either code-signing or switching to an MSIX / raw-folder distribution. Track reports and revisit.
- **Bundle size**: The one-folder bundle is ~500 MB without CUDA DLLs and ~1.2 GB with them. The Inno Setup installer compresses this to ~400–800 MB with `lzma2/max`.
- **First-launch delay**: See "Models" above. The post-install download step mitigates this, but the UX is still not great if the user skips it.
- **Model-download progress bar**: The wizard doesn't show one. If the selected model isn't cached, first launch can still be lengthy. The app now keeps the tray available and shows load status there; a dedicated wizard progress UI is still a v1.1 item.

## Verification checklist

Before announcing a release, smoke-test on a clean Windows VM:

- [ ] Installer launches and shows the disclaimer page.
- [ ] Default install path is `%LocalAppData%\Programs\Tachyon Transcripts`.
- [ ] Start Menu entry appears and launches the app.
- [ ] First run shows the setup wizard.
- [ ] Consent checkbox is required to proceed past the legal page.
- [ ] After finishing the wizard, the tray icon appears.
- [ ] Right-click → Start Recording produces a `mic.wav` and (if applicable) `system.wav` in the output folder.
- [ ] Uninstall via Settings → Apps removes the install tree but leaves recordings in the configured output folder (default: app-local `output/`).
- [ ] Re-install over an existing install works without errors.
