# whisp

Push-to-talk dictation for macOS. Hold **Fn**, speak, release. Text is typed
at the cursor. Runs entirely locally on Apple Silicon using Kyutai STT (MLX).
Lives in the menu bar, updates itself from your Forgejo repo on demand.

## Requirements

- Apple Silicon Mac (M-series), macOS 13+
- [`uv`](https://docs.astral.sh/uv/) (`brew install uv`)
- ~3 GB free for model weights on first run

## Two ways to run

### A. As a one-off script (dev mode)

```sh
uv run --script whisp.py
```

First run pulls deps and downloads ~2 GB of weights. Subsequent launches are
~5 seconds. You'll see a menu bar item with a dot. Hold **Fn** to dictate.

### B. As a real .app from Forgejo (installed mode)

After CI builds and publishes a release (see below):

1. Grab `Whisp.app.zip` from
   `https://git.retardhub.com/ahtavarasmus/whisp/releases/tag/latest`
2. Unzip, drag `Whisp.app` to `/Applications` (or `~/Applications`)
3. Right-click -> **Open** the first time (Gatekeeper: unsigned)
4. Grant Microphone and Accessibility in System Settings -> Privacy & Security
5. Click the menu bar dot -> **Check for updates** any time to pull the
   latest `whisp.py` from `main` and restart in place

## Layout

```
whisp.py                          # daemon (single file, uv PEP 723 deps)
app/
  Info.plist                      # bundle metadata template
  launcher.sh                     # bash entry point inside the .app
scripts/
  build_app.sh                    # assembles Whisp.app from sources
  publish_release.sh              # uploads the .zip to a Forgejo release
.forgejo/workflows/build.yaml     # CI: build on push to main
com.ahtava.whisp.plist            # optional LaunchAgent (run-on-login)
```

## How updates work

- `whisp.py` carries a `__VERSION__` stamp that `build_app.sh` rewrites with
  the git commit SHA.
- The .app launcher copies the bundled script to
  `~/Library/Application Support/Whisp/whisp.py` on first launch, then always
  runs that copy. Self-updates overwrite the copy.
- **Check for updates** in the menu bar hits Forgejo's
  `/api/v1/repos/.../commits` endpoint, compares to `__VERSION__`, and if
  newer, downloads `whisp.py` from `raw/branch/main/whisp.py`, writes it, and
  `os.execv`s the same Python on the new file.
- The .app bundle itself rarely needs replacing - only when you change
  `Info.plist`, `launcher.sh`, or want a clean install.

## Push to Forgejo and turn on CI

```sh
cd /Users/ahtavarasmus/Developer/whips
git init
git add .
git commit -m "initial commit"
git remote add origin https://git.retardhub.com/ahtavarasmus/whisp.git
git push -u origin main
```

Then in Forgejo:

1. Repo Settings -> Actions -> enable for this repo
2. Repo Settings -> Secrets -> add `FORGEJO_TOKEN` (a personal access token
   with `repo` write scope - needed to create releases)
3. Make sure a runner is online (Site Admin -> Actions -> Runners)

The workflow runs `ubuntu-latest`. The .app is just a directory tree plus
a bash launcher, so no macOS runner is needed for the build.

## Local build (test CI output)

```sh
bash scripts/build_app.sh
open dist/Whisp.app
```

## Flags

| Flag | What |
|---|---|
| `--hf-repo REPO` | Different STT model. Default `kyutai/stt-1b-en_fr-mlx` (1B, EN+FR, ~0.5s delay). Try `kyutai/stt-2.6b-en-mlx` for higher accuracy at ~2.5s delay. |
| `--device IDX` | Input device index. `python -m sounddevice` lists them. |
| `--version` | Print the build's `__VERSION__` stamp and exit. |
| `-v` | Echo recognised text to stderr as it streams. |

## Gotchas

- macOS built-in **Press Fn to Dictate** fights whisp for the key. Disable
  in `System Settings -> Keyboard -> Dictation`.
- Memory at idle: ~2 GB (bf16 weights resident).
- The .app is unsigned. First open requires right-click -> Open. To avoid
  that on every fresh download, run
  `xattr -dr com.apple.quarantine Whisp.app` after unzipping.
- Permissions follow the launching app. The .app gets them tied to the
  uv-vendored Python binary, which moves between builds, so granting
  permission once may not survive a `uv` cache wipe. If that happens,
  re-grant after the prompt.

## Credits

- [Kyutai STT](https://kyutai.org/stt) - the model.
- [delayed-streams-modeling](https://github.com/kyutai-labs/delayed-streams-modeling) - reference MLX implementation.
