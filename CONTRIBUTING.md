# Contributing to Tachyon Transcripts

Thanks for considering a contribution. This project is small and I'd like to keep the bar for "can a stranger pick this up and maintain it" high, so please read the short guidelines below before opening a PR.

## Reporting bugs

Open an issue using the **Bug report** template. The template asks for: Windows version, whether you built from source or installed the `.exe`, whether you have an NVIDIA GPU, what you were doing when the bug happened, and (crucially) the contents of `tachyon.log`. Without the log most runtime bugs are impossible to diagnose.

Never paste anything you recorded in a bug report — treat transcripts as private data.

## Requesting features

Open an issue using the **Feature request** template. Features that make the app more useful to non-technical users (better first-run UX, better install experience, clearer error messages) are most likely to get in before features that add raw capability.

## Development setup

Requires Python 3.11 on Windows 10/11 and (optionally) an NVIDIA GPU.

```
git clone https://github.com/PyroDS/projectTT.git
cd projectTT
setup.bat
```

`setup.bat` creates `.venv\` and installs everything. To launch the app in development:

```
run.bat
```

For interactive debugging, launch with a console instead of `pythonw`:

```
.venv\Scripts\python -m tachyon.main
```

To rebuild the installer:

```
installer\build_installer.bat
```

You'll need [Inno Setup 6](https://jrsoftware.org/isdl.php) on PATH.

## Documentation is part of the code

Every agent (human or AI) must read and update the three living documents:

- `CLAUDE.md` — agent rules.
- `docs/implementation-plan.md` — the technical spec.
- `docs/architecture.md` — the system design.
- `docs/development-log.md` — what's been done, decisions, open issues.

Before starting any non-trivial change, read the relevant docs. After finishing, update them. A PR that ships code but leaves the docs out of sync will be asked to fix the docs before merge.

## Coding conventions

- Python 3.11+, type hints on all public function signatures.
- `pathlib.Path`, not string paths.
- `dataclasses` for simple data structures.
- One module, one job. The `src/tachyon/` tree is flat-ish on purpose.
- Never call `tkinter` from a background thread. Use `queue.Queue` + `root.after()`.
- Never crash silently — log the error and notify the user via tray if appropriate.
- Use `logger = logging.getLogger(__name__)` at the top of each module.

## Pull request checklist

- [ ] Docs updated (implementation-plan, architecture, development-log).
- [ ] `python -m py_compile <every edited .py>` passes.
- [ ] I have manually tested the code path I changed.
- [ ] No new dependencies added without discussion in an issue first.
- [ ] No unrelated changes bundled in (small, focused PRs only).
- [ ] Commit message explains the *why*, not just the *what*.

## Legal contributions

If you propose changes to `docs/LEGAL.md`, especially adding or refining a jurisdiction, please cite the statute you're relying on. I'm not a lawyer and the doc is a good-faith summary — I'd rather keep it narrow and accurate than broad and confidently wrong.

## Code of conduct

Be kind. Don't be that person. If somebody opens an issue that's already been answered, point them at the answer without making them feel bad for not finding it first.
