#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12,<3.13"
# dependencies = [
#     "huggingface_hub",
#     "moshi_mlx==0.2.12",
#     "numpy",
#     "rustymimi",
#     "sentencepiece==0.2.0",
#     "sounddevice",
#     "pyobjc-framework-Quartz>=10",
#     "pyobjc-framework-Cocoa>=10",
#     "pyobjc-framework-ApplicationServices>=10",
#     "rumps>=0.4",
# ]
# ///
"""
whisp - push-to-talk dictation for macOS using Kyutai STT (MLX).

Hold the Fn key, speak, release. Transcribed text is typed at the cursor.
Lives in the menu bar with a Check-for-updates button.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import signal
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

# ---------- Build-time version stamp ----------
# Replaced by scripts/build_app.sh with the git commit SHA at build time.
__VERSION__ = "__VERSION_PLACEHOLDER__"

# ---------- Update endpoints ----------
REPO_OWNER = "ahtavarasmus"
REPO_NAME = "whisp"
FORGEJO_HOST = "git.retardhub.com"
REPO_API = f"https://{FORGEJO_HOST}/api/v1/repos/{REPO_OWNER}/{REPO_NAME}"
RAW_SCRIPT_URL = (
    f"https://{FORGEJO_HOST}/{REPO_OWNER}/{REPO_NAME}/raw/branch/main/whisp.py"
)

# ---------- Quartz / Carbon bridge ----------
import Quartz
from Quartz import (
    CFMachPortCreateRunLoopSource,
    CFRunLoopAddSource,
    CFRunLoopGetCurrent,
    CGEventCreateKeyboardEvent,
    CGEventGetFlags,
    CGEventKeyboardSetUnicodeString,
    CGEventPost,
    CGEventSourceCreate,
    CGEventTapCreate,
    CGEventTapEnable,
    kCFRunLoopCommonModes,
)

KCG_EVENT_FLAGS_CHANGED = 12
KCG_SESSION_EVENT_TAP = 1
KCG_HEAD_INSERT_EVENT_TAP = 0
KCG_TAP_OPTION_DEFAULT = 0
KCG_EVENT_SOURCE_HID = 1
KCG_ANNOTATED_SESSION_EVENT_TAP = 2
KCG_FLAG_MASK_FN = 0x00800000  # NSEventModifierFlagFunction

# ---------- Audio + model constants ----------
SAMPLE_RATE = 24_000
BLOCK_SIZE = 1_920  # 80 ms blocks
FLUSH_BLOCKS = 8  # ~640 ms silence after release to drain model delay

# ---------- App support paths ----------
APP_SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "Whisp"
LOG_FILE = Path.home() / "Library" / "Logs" / "whisp.log"


@dataclass
class Config:
    hf_repo: str = "kyutai/stt-1b-en_fr-mlx"
    max_steps: int = 4096
    device_index: int | None = None
    verbose: bool = False


def log(*args, **kwargs):
    print("[whisp]", *args, **kwargs, file=sys.stderr, flush=True)


# ---------- Text injection via CGEvent ----------
def type_unicode(text: str) -> None:
    if not text:
        return
    src = CGEventSourceCreate(KCG_EVENT_SOURCE_HID)
    CHUNK = 20
    for i in range(0, len(text), CHUNK):
        piece = text[i : i + CHUNK]
        down = CGEventCreateKeyboardEvent(src, 0, True)
        CGEventKeyboardSetUnicodeString(down, len(piece), piece)
        CGEventPost(KCG_ANNOTATED_SESSION_EVENT_TAP, down)
        up = CGEventCreateKeyboardEvent(src, 0, False)
        CGEventKeyboardSetUnicodeString(up, len(piece), piece)
        CGEventPost(KCG_ANNOTATED_SESSION_EVENT_TAP, up)


# ---------- Kyutai STT model wrapper ----------
class Transcriber:
    def __init__(self, cfg: Config):
        import mlx.core as mx
        import mlx.nn as nn
        import rustymimi
        import sentencepiece
        from huggingface_hub import hf_hub_download
        from moshi_mlx import models, utils

        self.mx = mx
        self.models = models
        self.utils = utils

        log(f"loading config from {cfg.hf_repo}")
        lm_config_path = hf_hub_download(cfg.hf_repo, "config.json")
        with open(lm_config_path, "r") as f:
            lm_config_raw = json.load(f)
        mimi_path = hf_hub_download(cfg.hf_repo, lm_config_raw["mimi_name"])
        moshi_name = lm_config_raw.get("moshi_name", "model.safetensors")
        moshi_path = hf_hub_download(cfg.hf_repo, moshi_name)
        tokenizer_path = hf_hub_download(cfg.hf_repo, lm_config_raw["tokenizer_name"])

        log("building model")
        lm_config = models.LmConfig.from_config_dict(lm_config_raw)
        self.lm_config = lm_config
        model = models.Lm(lm_config)
        model.set_dtype(mx.bfloat16)
        if moshi_path.endswith(".q4.safetensors"):
            nn.quantize(model, bits=4, group_size=32)
        elif moshi_path.endswith(".q8.safetensors"):
            nn.quantize(model, bits=8, group_size=64)
        log(f"loading weights from {os.path.basename(moshi_path)}")
        if cfg.hf_repo.endswith("-candle"):
            model.load_pytorch_weights(moshi_path, lm_config, strict=True)
        else:
            model.load_weights(moshi_path, strict=True)

        log("loading tokenizer")
        self.text_tokenizer = sentencepiece.SentencePieceProcessor(tokenizer_path)

        log("loading audio tokenizer (mimi)")
        mimi_codebooks = max(lm_config.generated_codebooks, lm_config.other_codebooks)
        self.audio_tokenizer = rustymimi.Tokenizer(mimi_path, num_codebooks=mimi_codebooks)
        self.other_codebooks = lm_config.other_codebooks

        log("warming up")
        model.warmup()
        self.model = model
        self.cfg = cfg
        log("model ready")

    def make_gen(self):
        return self.models.LmGen(
            model=self.model,
            max_steps=self.cfg.max_steps,
            text_sampler=self.utils.Sampler(top_k=25, temp=0),
            audio_sampler=self.utils.Sampler(top_k=250, temp=0.8),
            check=False,
        )

    def step(self, gen, pcm_block: np.ndarray) -> str | None:
        block = pcm_block[None, :, 0]
        other_audio_tokens = self.audio_tokenizer.encode_step(block[None, 0:1])
        other_audio_tokens = self.mx.array(other_audio_tokens).transpose(0, 2, 1)[
            :, :, : self.other_codebooks
        ]
        text_token = gen.step(other_audio_tokens[0])
        text_token = text_token[0].item()
        if text_token in (0, 3):
            return None
        piece = self.text_tokenizer.id_to_piece(text_token)
        return piece.replace("▁", " ")


# ---------- Engine: audio + model + typing ----------
class Engine:
    def __init__(self, transcriber: Transcriber, cfg: Config):
        self.t = transcriber
        self.cfg = cfg
        self.audio_q: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self.active = False
        self.session_id = 0
        self.flush_remaining = 0
        self._worker = threading.Thread(target=self._run_worker, daemon=True)
        self._stream = None
        self.on_state_change = lambda recording: None  # set by menu bar

    def start(self):
        import sounddevice as sd

        def cb(indata, _frames, _time, _status):
            with self._lock:
                if self.active:
                    self.audio_q.put(indata.copy())
                elif self.flush_remaining > 0:
                    self.audio_q.put(np.zeros_like(indata))
                    self.flush_remaining -= 1

        self._stream = sd.InputStream(
            channels=1,
            dtype="float32",
            samplerate=SAMPLE_RATE,
            blocksize=BLOCK_SIZE,
            callback=cb,
            device=self.cfg.device_index,
        )
        self._stream.start()
        self._worker.start()
        log("audio stream open; press and hold Fn to dictate")

    def stop(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()

    def on_key_down(self):
        notify = False
        with self._lock:
            if self.active:
                return
            self.active = True
            self.session_id += 1
            self.flush_remaining = 0
            self.audio_q.put(("__new__", self.session_id))
            notify = True
        if notify:
            try:
                self.on_state_change(True)
            except Exception as e:
                log("state callback error:", e)
        if self.cfg.verbose:
            log("Fn DOWN -> session", self.session_id)

    def on_key_up(self):
        notify = False
        with self._lock:
            if not self.active:
                return
            self.active = False
            self.flush_remaining = FLUSH_BLOCKS
            notify = True
        if notify:
            try:
                self.on_state_change(False)
            except Exception as e:
                log("state callback error:", e)
        if self.cfg.verbose:
            log("Fn UP  -> flushing")

    def _run_worker(self):
        gen = None
        first_token_in_session = True
        while True:
            item = self.audio_q.get()
            if item is None:
                return
            if isinstance(item, tuple) and item and item[0] == "__new__":
                gen = self.t.make_gen()
                first_token_in_session = True
                continue
            if gen is None:
                continue
            try:
                piece = self.t.step(gen, item)
            except Exception as e:
                log("step error:", e)
                continue
            if not piece:
                continue
            if first_token_in_session:
                piece = piece.lstrip()
                if not piece:
                    continue
                first_token_in_session = False
            type_unicode(piece)
            if self.cfg.verbose:
                print(piece, end="", flush=True)


# ---------- Fn-key event tap ----------
def install_fn_tap(engine: Engine):
    state = {"down": False}

    def tap_callback(_proxy, _type, event, _refcon):
        try:
            flags = CGEventGetFlags(event)
            fn_down = bool(flags & KCG_FLAG_MASK_FN)
            if fn_down and not state["down"]:
                state["down"] = True
                engine.on_key_down()
            elif not fn_down and state["down"]:
                state["down"] = False
                engine.on_key_up()
        except Exception as e:
            log("tap_callback error:", e)
        return event

    mask = 1 << KCG_EVENT_FLAGS_CHANGED
    tap = CGEventTapCreate(
        KCG_SESSION_EVENT_TAP,
        KCG_HEAD_INSERT_EVENT_TAP,
        KCG_TAP_OPTION_DEFAULT,
        mask,
        tap_callback,
        None,
    )
    if not tap:
        log("Failed to create event tap.")
        log("Grant Accessibility to whichever app launched this script:")
        log("  System Settings > Privacy & Security > Accessibility")
        sys.exit(1)

    src = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetCurrent(), src, kCFRunLoopCommonModes)
    CGEventTapEnable(tap, True)
    return tap


# ---------- Self-update from Forgejo ----------
def _http_get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "whisp-updater"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def latest_remote_sha() -> str:
    data = _http_get(f"{REPO_API}/commits?limit=1&sha=main")
    commits = json.loads(data)
    if not commits:
        raise RuntimeError("no commits returned")
    return commits[0]["sha"]


def fetch_remote_script() -> str:
    return _http_get(RAW_SCRIPT_URL, timeout=60).decode("utf-8")


def script_path() -> Path:
    """Path to *this running script* — the file we'll overwrite on update."""
    return Path(__file__).resolve()


def apply_update_and_restart(new_code: str, new_sha: str) -> None:
    """Overwrite this script in place and re-exec ourselves."""
    path = script_path()
    # Inject the new sha as the version stamp so the next launch knows itself.
    stamped = new_code.replace('__VERSION__ = "__VERSION_PLACEHOLDER__"', f'__VERSION__ = "{new_sha}"', 1)
    if stamped == new_code:
        # Remote already had a stamp baked in - leave as is.
        stamped = new_code
    path.write_text(stamped)
    os.chmod(path, 0o755)
    log(f"updated to {new_sha[:7]}; re-launching")
    # Re-exec the same script via the original argv. uv (if it's the parent) will
    # keep waiting on this PID across the execv.
    os.execv(sys.executable, [sys.executable, str(path), *sys.argv[1:]])


# ---------- Menu bar UI ----------
def make_menubar(engine: Engine):
    import rumps
    from PyObjCTools import AppHelper
    from AppKit import (
        NSApplication,
        NSApplicationActivationPolicyAccessory,
    )

    NSApplication.sharedApplication().setActivationPolicy_(
        NSApplicationActivationPolicyAccessory
    )

    class WhispApp(rumps.App):
        def __init__(self):
            short_sha = __VERSION__[:7] if __VERSION__ else "dev"
            super().__init__("Whisp", title="●", quit_button=None)
            self._idle_title = "●"
            self._rec_title = "● REC"
            self.menu = [
                rumps.MenuItem(f"whisp {short_sha}", callback=None),
                None,
                rumps.MenuItem("Check for updates", callback=self.on_check_updates),
                rumps.MenuItem("Show log", callback=self.on_show_log),
                None,
                rumps.MenuItem("Quit", callback=self.on_quit),
            ]

        def set_recording(self, recording: bool):
            self.title = self._rec_title if recording else self._idle_title

        def on_check_updates(self, _):
            threading.Thread(target=self._do_update_check, daemon=True).start()

        def _do_update_check(self):
            try:
                sha = latest_remote_sha()
            except (urllib.error.URLError, RuntimeError, json.JSONDecodeError) as e:
                AppHelper.callAfter(
                    rumps.notification,
                    "Whisp",
                    "Update check failed",
                    str(e),
                )
                return
            if sha == __VERSION__:
                AppHelper.callAfter(
                    rumps.notification,
                    "Whisp",
                    "Up to date",
                    f"On {sha[:7]}",
                )
                return
            try:
                code = fetch_remote_script()
            except urllib.error.URLError as e:
                AppHelper.callAfter(
                    rumps.notification,
                    "Whisp",
                    "Download failed",
                    str(e),
                )
                return
            AppHelper.callAfter(
                rumps.notification,
                "Whisp",
                f"Updating to {sha[:7]}",
                "Restarting…",
            )
            # Give the notification a moment to render before we exec.
            threading.Timer(
                0.8, lambda: apply_update_and_restart(code, sha)
            ).start()

        def on_show_log(self, _):
            os.system(f"open -a Console {LOG_FILE!s} 2>/dev/null || open {LOG_FILE!s}")

        def on_quit(self, _):
            try:
                engine.stop()
            finally:
                rumps.quit_application()

    app = WhispApp()
    engine.on_state_change = lambda rec: AppHelper.callAfter(app.set_recording, rec)
    return app


# ---------- Entrypoint ----------
def parse_args(argv: list[str]) -> Config:
    p = argparse.ArgumentParser(prog="whisp", description=__doc__)
    p.add_argument("--hf-repo", default="kyutai/stt-1b-en_fr-mlx")
    p.add_argument("--max-steps", type=int, default=4096)
    p.add_argument("--device", type=int, default=None)
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="store_true", help="Print version and exit.")
    a = p.parse_args(argv)
    if a.version:
        print(__VERSION__)
        sys.exit(0)
    return Config(
        hf_repo=a.hf_repo,
        max_steps=a.max_steps,
        device_index=a.device,
        verbose=a.verbose,
    )


def main(argv: list[str] | None = None) -> int:
    cfg = parse_args(sys.argv[1:] if argv is None else argv)
    APP_SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    t = Transcriber(cfg)
    engine = Engine(t, cfg)
    engine.start()
    install_fn_tap(engine)
    app = make_menubar(engine)

    def shutdown(_signum, _frame):
        log("shutting down")
        engine.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log(f"running v{__VERSION__[:7] if __VERSION__ else 'dev'}; hold Fn to dictate")
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
