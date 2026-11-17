import json
import multiprocessing
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback
import wave
from datetime import datetime
from pathlib import Path

import webview

from paths import APP_NAME, IS_MAC, IS_WIN, glossary_path, output_dir, resource_dir, voice_profile_path

VERSION = "1.0.0"
AUDIO_EXT = ("m4a", "mp3", "wav", "aac", "flac", "ogg", "opus", "mp4", "mov", "webm", "wma", "amr")
LINE_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] \[([^\]]+)\] (.*)$")


def open_path(p):
    p = str(p)
    if IS_WIN:
        os.startfile(p)
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def ui_path() -> str:
    for base in (resource_dir() / "app" / "ui", resource_dir() / "ui", Path(__file__).resolve().parent / "ui"):
        if (base / "index.html").exists():
            return str(base / "index.html")
    raise FileNotFoundError("ui/index.html")


class Recorder:
    def __init__(self):
        self.stream = None
        self.wav = None
        self.path = None
        self.level = 0.0
        self.started = 0.0
        self.rate = 16000
        self.listener = None

    def start(self, path: Path, listener=None):
        import numpy as np
        import sounddevice as sd

        def callback(indata, frames, t, status):
            mono = indata[:, 0]
            self.wav.writeframes((np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes())
            self.level = float(np.sqrt(np.mean(mono ** 2)))
            if self.listener:
                self.listener(mono.copy(), self.rate)

        last_error = None
        for rate in (16000, 48000, 44100):
            try:
                self.wav = wave.open(str(path), "wb")
                self.wav.setnchannels(1)
                self.wav.setsampwidth(2)
                self.wav.setframerate(rate)
                self.rate = rate
                self.listener = listener
                self.stream = sd.InputStream(samplerate=rate, channels=1, dtype="float32", callback=callback)
                self.stream.start()
                self.path = path
                self.started = time.time()
                return
            except Exception as e:
                last_error = e
                if self.wav:
                    self.wav.close()
                path.unlink(missing_ok=True)
        raise RuntimeError(f"마이크를 열 수 없어요. 마이크 사용 권한을 확인해 주세요. ({last_error})")

    def stop(self) -> Path:
        self.stream.stop()
        self.stream.close()
        self.wav.close()
        self.stream = None
        self.listener = None
        return self.path

    @property
    def active(self):
        return self.stream is not None


def parse_transcript(path: Path) -> dict:
    meta, lines = {}, []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("# 원본:"):
            meta["source"] = raw.split(":", 1)[1].strip()
        elif raw.startswith("# 길이:"):
            m = re.search(r"(\d+)분.*?(\d+)명", raw)
            if m:
                meta["minutes"], meta["count"] = int(m.group(1)), int(m.group(2))
        else:
            m = LINE_RE.match(raw)
            if m:
                lines.append({"t": m.group(1), "who": m.group(2), "text": m.group(3)})
    speakers = []
    for l in lines:
        if l["who"] not in speakers:
            speakers.append(l["who"])
    speakers.sort(key=lambda s: (s != "나", s))
    return {"meta": meta, "lines": lines, "speakers": speakers}


def title_of(path: Path) -> str:
    stem = re.sub(r"^\d{4}-\d{2}-\d{2}_\d{4}_", "", path.stem)
    m = re.match(r"회의_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})\d{2}$", stem)
    return f"{int(m.group(2))}월 {int(m.group(3))}일 {m.group(4)}:{m.group(5)} 회의" if m else stem


def date_of(path: Path) -> str:
    d = datetime.fromtimestamp(path.stat().st_mtime)
    return f"{d.year}. {d.month}. {d.day}. {'오전' if d.hour < 12 else '오후'} {(d.hour % 12) or 12}:{d.minute:02d}"


class Api:
    def __init__(self):
        self._window = None
        self._engine_obj = None
        self._jobs = queue.Queue()
        self._recorder = Recorder()
        self._voice_recorder = Recorder()
        self._live = None
        self._current = None
        self._lock = threading.Lock()
        threading.Thread(target=self._worker, daemon=True).start()
        threading.Thread(target=self._pump, daemon=True).start()

    def _emit(self, event: dict):
        if self._window:
            try:
                self._window.evaluate_js(f"window.onEvent && onEvent({json.dumps(event, ensure_ascii=False)})")
            except Exception:
                pass

    def _engine(self):
        with self._lock:
            if self._engine_obj is None:
                from engine import Engine
                self._engine_obj = Engine(self._engine_log)
            return self._engine_obj

    def _engine_log(self, msg=None, progress=None, done=None):
        job = self._current
        if not job:
            return
        if msg:
            if msg.startswith("받아쓰기 "):
                job["step"], job["msg"] = 1, f"받아쓰기 {msg.split(' ', 1)[1]} 구간"
            elif msg.startswith("화자 분리"):
                job["step"], job["msg"] = 0, "목소리로 화자를 구분하는 중"
            elif not msg.startswith(("처리 시작", "길이", "완료")):
                job["msg"] = msg
        if progress is not None:
            job["pct"] = progress * 0.35 if job["step"] == 0 else 0.35 + progress * 0.65
        job["queue"] = self._jobs.qsize()
        self._emit({"type": "job", "job": job})

    def _worker(self):
        while True:
            kind, path, count = self._jobs.get()
            try:
                if kind == "voice":
                    self._engine().register_voice(path)
                    self._emit({"type": "voice"})
                    continue
                self._current = {"name": Path(path).name, "msg": "준비 중", "pct": 0, "step": 0, "queue": self._jobs.qsize()}
                self._emit({"type": "job", "job": self._current})
                out = self._engine().process(path, count)
                self._current = None
                self._emit({"type": "done", "path": str(out)})
            except Exception as e:
                traceback.print_exc()
                self._current = None
                self._emit({"type": "error", "msg": f"처리하지 못했어요: {e}"})

    def _pump(self):
        while True:
            time.sleep(0.1)
            for rec, voice in ((self._recorder, False), (self._voice_recorder, True)):
                if rec.active:
                    self._emit({"type": "level", "voice": voice, "level": rec.level, "elapsed": time.time() - rec.started})

    def get_state(self):
        recent = []
        for p in sorted(output_dir().glob("*.txt"), key=lambda x: x.stat().st_mtime, reverse=True)[:50]:
            if p.name == "용어.txt":
                continue
            try:
                info = parse_transcript(p)
            except Exception:
                continue
            src = info["meta"].get("source", "")
            recent.append({
                "path": str(p), "title": title_of(p), "date": date_of(p),
                "kind": "rec" if src.startswith("회의_") else "file",
                "minutes": info["meta"].get("minutes", 0), "speakers": len(info["speakers"]),
            })
        return {
            "voice": voice_profile_path().exists(), "notice": (output_dir() / ".notice_ok").exists(),
            "version": VERSION, "engine": "Apple 실리콘 가속" if IS_MAC else "Windows", "recent": recent,
            "job": self._current,
        }

    def notice_ok(self):
        (output_dir() / ".notice_ok").write_text("ok")

    def start_record(self, count=None):
        if self._recorder.active or self._voice_recorder.active:
            return "이미 녹음 중이에요"
        path = output_dir() / "녹음" / f"회의_{datetime.now():%Y%m%d_%H%M%S}.wav"
        try:
            from engine import LiveTranscriber
            self._live = LiveTranscriber(
                self._engine(),
                on_lines=lambda lines: self._emit({"type": "live", "lines": lines}),
                on_status=lambda msg: self._emit({"type": "live_status", "msg": msg}),
            )
            self._recorder.start(path, listener=self._live.feed)
        except Exception as e:
            if self._live:
                self._live.stop()
                self._live = None
            return str(e)
        return True

    def stop_record(self, count=None):
        if not self._recorder.active:
            return False
        if self._live:
            self._live.stop()
            self._live = None
        self._jobs.put(("meeting", self._recorder.stop(), count))
        return True

    def cancel_record(self):
        if self._live:
            self._live.stop()
            self._live = None
        if self._recorder.active:
            path = self._recorder.stop()
            try:
                path.unlink()
            except OSError:
                pass
        return True

    def pick_files(self, count=None):
        types = ("녹음 파일 (" + ";".join(f"*.{e}" for e in AUDIO_EXT) + ")", "모든 파일 (*.*)")
        files = self._window.create_file_dialog(webview.OPEN_DIALOG, allow_multiple=True, file_types=types) or []
        for f in files:
            self._jobs.put(("meeting", Path(f), count))
        return len(files)

    def read_transcript(self, path):
        p = Path(path)
        info = parse_transcript(p)
        return {
            "path": str(p), "title": title_of(p), "date": date_of(p),
            "minutes": info["meta"].get("minutes", 0), "speakers": info["speakers"], "lines": info["lines"],
        }

    def rename_speaker(self, path, old, new):
        p = Path(path)
        new = new.replace("[", "").replace("]", "").strip()
        text = p.read_text(encoding="utf-8")
        text = re.sub(rf"(?m)^(\[\d\d:\d\d:\d\d\]) \[{re.escape(old)}\] ", lambda m: f"{m.group(1)} [{new}] ", text)
        p.write_text(text, encoding="utf-8")
        return True

    def copy_text(self, path):
        text = Path(path).read_text(encoding="utf-8")
        try:
            if IS_WIN:
                subprocess.run(["clip"], input=text.encode("utf-16le"), check=True, creationflags=0x08000000)
                return True
            if IS_MAC:
                subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
                return True
        except Exception:
            pass
        return text

    def open_path(self, path):
        open_path(path)

    def open_folder(self):
        open_path(output_dir())

    def voice_start(self):
        if self._recorder.active or self._voice_recorder.active:
            return "녹음 중에는 등록할 수 없어요"
        try:
            self._voice_recorder.start(output_dir() / "녹음" / "내목소리.wav")
        except Exception as e:
            return str(e)
        return True

    def voice_stop(self):
        if self._voice_recorder.active:
            self._jobs.put(("voice", self._voice_recorder.stop(), None))
        return True

    def voice_cancel(self):
        if self._voice_recorder.active:
            self._voice_recorder.stop()
        return True

    def get_glossary(self):
        lines = glossary_path().read_text(encoding="utf-8").splitlines()
        return "\n".join(l for l in lines if not l.startswith("#")).strip()

    def save_glossary(self, text):
        head = [l for l in glossary_path().read_text(encoding="utf-8").splitlines() if l.startswith("#")]
        glossary_path().write_text("\n".join(head + [text.strip()]) + "\n", encoding="utf-8")
        return True


def main():
    multiprocessing.freeze_support()
    api = Api()
    window = webview.create_window(APP_NAME, url=ui_path(), js_api=api, width=1180, height=800,
                                   min_size=(900, 640), background_color="#f4f6f9")
    api._window = window

    def on_closing():
        if api._recorder.active:
            api.stop_record()
        return True

    window.events.closing += on_closing
    webview.start(gui="edgechromium" if IS_WIN else None, debug="--debug" in sys.argv)
    os._exit(0)


if __name__ == "__main__":
    main()
