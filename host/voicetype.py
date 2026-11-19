#!/usr/bin/env python3
"""
voicetype — push-to-talk dictation from the Atech Tiny voice stick.

Hold the stick's button and talk. Each phrase is typed wherever your cursor is
as soon as you pause — you don't have to let go first. Typing uses xdotool on
Linux (X11) and the clipboard + System Events on macOS.
Transcription runs locally: faster-whisper, or mlx-whisper on Apple Silicon.

    host/.venv/bin/python host/voicetype.py                   # default: base.en, live
                                                              # (Apple Silicon: large-v3-turbo, any language)
    host/.venv/bin/python host/voicetype.py --language it     # force a language
    host/.venv/bin/python host/voicetype.py --model small.en  # more accurate, ~3x slower
    host/.venv/bin/python host/voicetype.py --enter           # press Enter on release
                                                              # (or tap the button: Enter / Tab)
    host/.venv/bin/python host/voicetype.py --no-live         # type everything on release

See ../firmware/src/main.cpp for the wire protocol.
"""

from __future__ import annotations

import argparse
import logging
import os
import platform
import queue
import shutil
import struct
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass

import numpy as np
import serial
from serial.tools import list_ports

SAMPLE_RATE = 16000
BYTES_PER_SEC = SAMPLE_RATE * 2
MAGIC = b"\xa5\x5a"
ESPRESSIF_VID = 0x303A
MAX_PAYLOAD = 4096
PACKET_TYPES = b"SDEANT"
STALL_SECONDS = 3.0  # a recording with no packets this long is finalized anyway

CLEAR_LINES = 6  # Ctrl+U presses when clearing a prompt; each clears one line

IS_MAC = sys.platform == "darwin"
APPLE_SILICON = IS_MAC and platform.machine() == "arm64"
MLX_DEFAULT_MODEL = "mlx-community/whisper-large-v3-turbo"

DEFAULT_PROMPT = (
    "Dictation for a coding assistant in a terminal. Claude, Claude Code, git, "
    "GitHub, Python, TypeScript, JSON, API, CLI, README, npm, pip, ESP32, Atech."
)

log = logging.getLogger("voicetype")


# ---------------------------------------------------------------- serial link
def find_port() -> str | None:
    for p in list_ports.comports():
        if p.vid == ESPRESSIF_VID:
            return p.device
    return None


def open_port(device: str) -> serial.Serial:
    s = serial.Serial()
    s.port = device
    s.baudrate = 115200
    s.timeout = 0.1
    # Keep DTR/RTS low: on the ESP32-S3's USB-Serial-JTAG, some DTR/RTS
    # combinations reset the chip.
    s.dtr = False
    s.rts = False
    s.open()
    return s


def parse_packets(buf: bytearray):
    """Yield (type, payload) from buf, consuming what it parses. Non-packet
    bytes (driver log lines) are logged at debug level and discarded."""
    while True:
        i = buf.find(MAGIC)
        if i < 0:
            junk_end = len(buf) - 1 if buf.endswith(MAGIC[:1]) else len(buf)
            if junk_end:
                _log_junk(buf[:junk_end])
                del buf[:junk_end]
            return
        if i:
            _log_junk(buf[:i])
            del buf[:i]
        if len(buf) < 5:
            return
        ptype = buf[2]
        (length,) = struct.unpack_from("<H", buf, 3)
        if ptype not in PACKET_TYPES or length > MAX_PAYLOAD:
            del buf[:1]  # false magic; resync
            continue
        if len(buf) < 5 + length:
            return
        payload = bytes(buf[5 : 5 + length])
        del buf[: 5 + length]
        yield chr(ptype), payload


def _log_junk(b: bytes) -> None:
    text = b.decode("utf-8", "replace").strip()
    if text:
        log.debug("device: %s", text)


# -------------------------------------------------------------- transcription
class LocalWhisper:
    def __init__(self, model: str, prompt: str | None, threads: int, language: str | None = "en"):
        from faster_whisper import WhisperModel

        log.info("loading whisper model %r (first run downloads it)...", model)
        t0 = time.time()
        self.model = WhisperModel(model, device="cpu", compute_type="int8", cpu_threads=threads)
        self.prompt = prompt
        self.language = language
        log.info("model ready in %.1fs", time.time() - t0)

    def transcribe(self, audio: np.ndarray, context: str = "") -> str:
        # Feeding the words already typed in this dictation keeps phrases
        # consistent (capitalisation, punctuation, names) across the splits.
        prompt = " ".join(p for p in (self.prompt, context[-200:]) if p) or None
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            beam_size=5,
            vad_filter=True,
            condition_on_previous_text=False,
            initial_prompt=prompt,
        )
        return " ".join(s.text.strip() for s in segments).strip()


class MLXWhisper:
    """Whisper on the Apple Silicon GPU (mlx-whisper). Fast enough for
    large-v3-turbo, which is multilingual and more accurate than base.en."""

    def __init__(self, model: str, prompt: str | None, language: str | None):
        import mlx_whisper

        self._mlx = mlx_whisper
        self.model = model
        self.prompt = prompt
        self.language = language
        log.info("loading mlx-whisper model %r (first run downloads it)...", model)
        t0 = time.time()
        self.transcribe(np.zeros(SAMPLE_RATE // 2, dtype=np.float32))   # load + warm up
        log.info("model ready in %.1fs", time.time() - t0)

    def transcribe(self, audio: np.ndarray, context: str = "") -> str:
        prompt = " ".join(p for p in (self.prompt, context[-200:]) if p) or None
        out = self._mlx.transcribe(
            audio,
            path_or_hf_repo=self.model,
            language=self.language,
            condition_on_previous_text=False,
            initial_prompt=prompt,
            verbose=None,
        )
        return out["text"].strip()


def prepare_audio(pcm: bytes) -> np.ndarray:
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    if a.size == 0:
        return a
    a -= a.mean()  # MEMS mics carry a small DC offset
    peak = float(np.abs(a).max())
    if peak > 1e-5:
        a *= min(0.9 / peak, 50.0)  # the mic is quiet at arm's length; normalize
    return a


VAD_GAIN = 4.0  # fixed: speech from this mic peaks near 0.25 FS; x4 keeps it unclipped


def vad_audio(pcm: bytes) -> np.ndarray:
    """Audio for voice detection: fixed gain, NOT peak-normalized. Normalizing a
    buffer of pure room noise blows the noise up until the VAD calls it speech."""
    a = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
    if a.size:
        a -= a.mean()
    return np.clip(a * VAD_GAIN, -1.0, 1.0)


# Whisper's well-known outputs for near-silence.
HALLUCINATIONS = {"", "you", "thank you", "thanks for watching", "thank you for watching", "bye",
                  # Italian ones, now that the multilingual model is an option
                  "grazie", "grazie a tutti", "grazie per la visione",
                  "sottotitoli creati dalla comunità amara.org", "sottotitoli a cura di qtss"}


def is_hallucination(text: str) -> bool:
    return text.lower().strip(" .!?,") in HALLUCINATIONS


# --------------------------------------------------------- live phrase splitter
class PhraseSplitter:
    """Cuts a growing recording into phrases at pauses, using Silero VAD (ships
    with faster-whisper). Each phrase can be transcribed while you keep talking."""

    CHECK_EVERY = 0.3   # seconds of new audio between VAD passes
    MAX_PHRASE = 20.0   # force a cut so one phrase never nears Whisper's 30 s window
    KEEP_LEAD = 0.5     # silence kept before speech so its onset isn't clipped

    def __init__(self, pause: float):
        from faster_whisper.vad import VadOptions, get_speech_timestamps

        self._vad = get_speech_timestamps
        self.pause = pause
        self.opts = VadOptions(min_silence_duration_ms=int(pause * 1000), speech_pad_ms=150)
        self.reset()

    def reset(self) -> None:
        self.pending = bytearray()
        self._unchecked = 0

    def feed(self, payload: bytes) -> bytes | None:
        """Add audio; return a finished phrase (raw PCM) if one just ended."""
        self.pending += payload
        self._unchecked += len(payload)
        if self._unchecked < self.CHECK_EVERY * BYTES_PER_SEC:
            return None
        self._unchecked = 0

        audio = vad_audio(bytes(self.pending))
        n = audio.size
        speech = self._vad(audio, self.opts)
        if not speech:
            # Only silence so far: drop it, keeping a little lead-in.
            keep = int(self.KEEP_LEAD * SAMPLE_RATE)
            if n > keep:
                del self.pending[: (n - keep) * 2]
            return None

        end = speech[-1]["end"]
        if n - end >= self.pause * SAMPLE_RATE:
            return self._cut(end)
        if n >= self.MAX_PHRASE * SAMPLE_RATE:
            # No pause long enough: cut at the last gap the VAD saw, or here.
            return self._cut(speech[-2]["end"] if len(speech) > 1 else n)
        return None

    def flush(self) -> bytes:
        rest = bytes(self.pending)
        self.reset()
        return rest

    def _cut(self, at_sample: int) -> bytes:
        at = at_sample * 2
        phrase = bytes(self.pending[:at])
        del self.pending[:at]
        return phrase


# ---------------------------------------------------------------------- typing
def type_text(text: str) -> None:
    if IS_MAC:
        _mac_paste(text)
        return
    subprocess.run(["xdotool", "type", "--clearmodifiers", "--delay", "4", "--", text], check=True)


def press_key(key: str, repeat: int = 1) -> None:
    if IS_MAC:
        _mac_key(key, repeat)
        return
    subprocess.run(["xdotool", "key", "--clearmodifiers", "--repeat", str(repeat), "--repeat-delay", "2", key],
                   check=True)


# macOS: System Events does the keystrokes. The terminal (or the Python that
# runs eve) needs Privacy & Security > Accessibility, or nothing gets typed.
MAC_KEY_CODES = {"Return": 36, "Tab": 48, "BackSpace": 51}


def _osascript(script: str) -> None:
    subprocess.run(["osascript", "-e", script], check=True, capture_output=True)


def _mac_paste(text: str) -> None:
    """Paste through the clipboard: unlike keystroke-typing, it handles accents
    and emoji and is instant. The previous (text) clipboard is put back."""
    old = subprocess.run(["pbpaste"], capture_output=True).stdout
    subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
    _osascript('tell application "System Events" to keystroke "v" using command down')
    time.sleep(0.15)   # let the app read the clipboard before restoring it
    subprocess.run(["pbcopy"], input=old)


def _mac_key(key: str, repeat: int) -> None:
    if key in MAC_KEY_CODES:
        action = f"key code {MAC_KEY_CODES[key]}"
    elif key.startswith("ctrl+") and len(key) == 6:
        action = f'keystroke "{key[-1]}" using control down'
    else:
        raise ValueError(f"no macOS mapping for key {key!r}")
    _osascript(f'tell application "System Events"\nrepeat {int(repeat)} times\n{action}\nend repeat\nend tell')


# ------------------------------------------------------------------------ main
@dataclass
class Job:
    dictation: int   # which button-hold this audio belongs to
    pcm: bytes
    final: bool      # last piece of the dictation (button released)
    kind: str = "audio"   # or "tap" / "double": button gestures, handled in order


class App:
    def __init__(self, args):
        self.args = args
        prompt = None if args.no_prompt else args.prompt
        if args.engine == "mlx":
            self.engine = MLXWhisper(args.model, prompt, args.language)
        else:
            self.engine = LocalWhisper(args.model, prompt, args.threads, args.language)
        self.splitter = None if args.no_live else PhraseSplitter(args.pause)
        self.jobs: queue.Queue[Job] = queue.Queue()
        self._carry: Job | None = None
        self.unsent = 0   # characters eve has typed that no Enter has sent yet
        self.tabbed = False  # eve pressed Tab (accepted a suggestion) that no Enter has sent yet
        self.port: serial.Serial | None = None
        self.port_lock = threading.Lock()

    def tell_device(self, code: bytes) -> None:
        with self.port_lock:
            try:
                if self.port and self.port.is_open:
                    self.port.write(code)
            except serial.SerialException:
                pass

    # -- worker: transcribe + type in order, off the serial thread
    def worker(self) -> None:
        current = -1
        context = ""      # text typed so far in the current dictation
        typed_any = False
        while True:
            job = self._next_job()
            if job.kind in ("tap", "double"):
                # Queued behind any text still being typed, so it acts after it.
                self.gesture(job.kind)
                continue
            if job.dictation != current:
                current, context, typed_any = job.dictation, "", False
            try:
                text = self.transcribe(job, context)
                if text:
                    text += " "   # phrases and dictations need a space between them
                    if self.args.dry_run:
                        print(text, end="" if not job.final else "\n", flush=True)
                    else:
                        type_text(text)
                    context += text
                    typed_any = True
                    self.unsent += len(text)
                if job.final:
                    if self.args.dry_run and not text:
                        print(flush=True)
                    if self.args.enter and typed_any and not self.args.dry_run:
                        press_key("Return")
                        self.unsent = 0
                        self.tabbed = False
                    self.tell_device(b"K" if typed_any else b"I")
            except Exception:
                log.exception("transcription failed")
                self.tell_device(b"X")

    def gesture(self, kind: str) -> None:
        """Single tap: Enter if eve has typed unsent text, else Tab (accepts
        Claude Code's suggestion). Double tap: delete eve's unsent text, or
        clear the whole prompt if a Tab put a suggestion there."""
        try:
            if kind == "tap":
                # A Tab fills in Claude Code's suggestion, so after one there is
                # text waiting too: the next tap sends it.
                send = bool(self.unsent) or self.tabbed
                key = "Return" if send else "Tab"
                log.info("tap -> %s", "Enter" if send else "Tab")
                if self.args.dry_run:
                    print(f"[{key}]", flush=True)
                else:
                    press_key(key)
                self.unsent = 0
                self.tabbed = not send
            elif self.tabbed:
                # eve can't know how long the accepted suggestion is, so clear
                # the whole prompt the documented Claude Code way: Ctrl+E to the
                # line end, then Ctrl+U (delete to line start) repeated, which
                # also clears multi-line input. Not Esc Esc: on an empty prompt
                # that opens the rewind menu.
                log.info("double tap -> clear the prompt (suggestion%s)",
                         f" + {self.unsent} dictated characters" if self.unsent else "")
                if self.args.dry_run:
                    print("[clear prompt]", flush=True)
                else:
                    press_key("ctrl+e")
                    press_key("ctrl+u", repeat=CLEAR_LINES)
                self.unsent = 0
                self.tabbed = False
            elif self.unsent:
                log.info("double tap -> delete %d characters", self.unsent)
                if self.args.dry_run:
                    print(f"[delete {self.unsent}]", flush=True)
                else:
                    press_key("BackSpace", repeat=self.unsent)
                self.unsent = 0
            else:
                log.info("double tap -> nothing to delete")
        except Exception:
            log.exception("%s failed", kind)
            self.tell_device(b"X")

    def _next_job(self) -> Job:
        """Next job, merged with any phrases of the same dictation already waiting.
        Each Whisper call has a fixed cost however short the audio, so when
        phrases arrive faster than they're transcribed, one call for all of
        them keeps the lag from building up."""
        job = self._carry or self.jobs.get()
        self._carry = None
        while not job.final:
            try:
                nxt = self.jobs.get_nowait()
            except queue.Empty:
                break
            if nxt.kind != "audio" or nxt.dictation != job.dictation:
                self._carry = nxt
                break
            job = Job(job.dictation, job.pcm + nxt.pcm, nxt.final)
        return job

    def transcribe(self, job: Job, context: str) -> str:
        seconds = len(job.pcm) / BYTES_PER_SEC
        if seconds < 0.3:
            return ""
        if job.final:
            self.tell_device(b"B")
        if self.args.save_wav:
            save_wav(self.args.save_wav, job.pcm)
        t0 = time.time()
        text = self.engine.transcribe(prepare_audio(job.pcm), context)
        dt = time.time() - t0
        if is_hallucination(text):
            log.info("%.1fs audio -> (nothing heard) [%.1fs]", seconds, dt)
            return ""
        log.info("%.1fs audio -> %r [%.1fs]%s", seconds, text, dt, "" if job.final else " (live)")
        return text

    # -- serial loop with automatic reconnect
    def run(self) -> None:
        threading.Thread(target=self.worker, daemon=True).start()
        waiting_logged = False
        while True:
            device = self.args.port or find_port()
            if not device:
                if not waiting_logged:
                    log.info("waiting for the voice stick to be plugged in...")
                    waiting_logged = True
                time.sleep(1)
                continue
            try:
                with self.port_lock:
                    self.port = open_port(device)
                log.info("connected on %s — hold the button and talk", device)
                waiting_logged = False
                self.read_loop()
            except serial.SerialException as e:
                log.warning("device disconnected (%s)", e)
            finally:
                with self.port_lock:
                    if self.port:
                        try:
                            self.port.close()
                        except Exception:
                            pass
                    self.port = None
            time.sleep(1)

    def read_loop(self) -> None:
        buf = bytearray()
        rec: bytearray | None = None   # whole recording, used by --no-live
        dictation = 0
        last_packet = time.time()

        def finish():
            nonlocal rec
            pcm = self.splitter.flush() if self.splitter else bytes(rec)
            self.jobs.put(Job(dictation, pcm, final=True))
            rec = None

        while True:
            data = self.port.read(4096)
            if data:
                buf += data
                for ptype, payload in parse_packets(buf):
                    last_packet = time.time()
                    if ptype == "S":
                        if rec is not None:
                            log.warning("new recording before the last one ended; finishing it")
                            finish()
                        dictation += 1
                        rec = bytearray()
                        if self.splitter:
                            self.splitter.reset()
                        log.info("recording...")
                    elif ptype == "D" and rec is not None:
                        if self.splitter:
                            phrase = self.splitter.feed(payload)
                            if phrase:
                                self.jobs.put(Job(dictation, phrase, final=False))
                        else:
                            rec += payload
                            if len(rec) > self.args.max_seconds * BYTES_PER_SEC:
                                log.warning("hit %ss cap, transcribing what we have", self.args.max_seconds)
                                finish()
                    elif ptype == "A" and rec is not None:
                        rec = None   # it was only a tap; nothing to transcribe
                        if self.splitter:
                            self.splitter.reset()
                    elif ptype == "T":
                        self.jobs.put(Job(-1, b"", True, kind="tap"))
                    elif ptype == "N":
                        self.jobs.put(Job(-1, b"", True, kind="double"))
                    elif ptype == "E" and rec is not None:
                        dropped = struct.unpack("<I", payload)[0] if len(payload) == 4 else 0
                        if dropped:
                            log.warning("device dropped %d samples (USB too slow?)", dropped)
                        finish()
            elif rec is not None and time.time() - last_packet > STALL_SECONDS:
                log.warning("stream stalled; transcribing what arrived")
                finish()


def save_wav(path: str, pcm: bytes) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--engine", choices=("auto", "faster", "mlx"), default="auto",
                    help="speech engine: mlx (Apple Silicon GPU), faster (faster-whisper, CPU). "
                         "auto = mlx on Apple Silicon if installed, else faster")
    ap.add_argument("--model", default=os.environ.get("VOICETYPE_MODEL"),
                    help="faster: tiny.en, base.en (default), small.en (more accurate, ~3x slower), medium.en, "
                         "or a multilingual one like small. mlx: a Hugging Face repo, default "
                         + MLX_DEFAULT_MODEL)
    ap.add_argument("--language", default=os.environ.get("VOICETYPE_LANGUAGE", "auto"),
                    help="spoken language code (en, it, ...) or auto (default). *.en models are English only")
    ap.add_argument("--port", help="serial device (default: auto-detect the ESP32-S3)")
    ap.add_argument("--enter", action="store_true", help="press Enter when you release the button (sends it to Claude)")
    ap.add_argument("--no-live", action="store_true", help="type everything at once on release instead of phrase by phrase")
    ap.add_argument("--pause", type=float, default=0.4,
                    help="seconds of silence that end a phrase in live mode (default 0.6)")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT, help="vocabulary hint for whisper")
    ap.add_argument("--no-prompt", action="store_true", help="disable the vocabulary hint")
    ap.add_argument("--threads", type=int, default=max(1, (os.cpu_count() or 4) // 2))
    ap.add_argument("--max-seconds", type=int, default=120, help="cap for one dictation in --no-live mode")
    ap.add_argument("--dry-run", action="store_true", help="print text instead of typing it")
    ap.add_argument("--save-wav", metavar="PATH", help="save each recording (debugging the mic)")
    ap.add_argument("-v", "--verbose", action="store_true", help="also show device log lines")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "huggingface_hub", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if args.engine == "auto":
        try:
            if not APPLE_SILICON:
                raise ImportError
            import mlx_whisper  # noqa: F401
            args.engine = "mlx"
        except ImportError:
            args.engine = "faster"
    if not args.model:
        args.model = MLX_DEFAULT_MODEL if args.engine == "mlx" else "base.en"
    if args.model.endswith(".en"):
        args.language = "en"
    elif args.language == "auto":
        args.language = None
    log.info("engine %s, model %s, language %s", args.engine, args.model, args.language or "auto")

    if args.dry_run:
        pass
    elif IS_MAC:
        log.info("macOS: if nothing gets typed, allow this terminal in "
                 "System Settings > Privacy & Security > Accessibility")
    else:
        if not shutil.which("xdotool"):
            sys.exit("xdotool is not installed: sudo apt install xdotool")
        if not os.environ.get("DISPLAY"):
            sys.exit("DISPLAY is not set — run this inside your X11 session")

    try:
        App(args).run()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
