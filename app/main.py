import multiprocessing
import os
import queue
import subprocess
import sys
import threading
import time
import traceback
import wave
from datetime import datetime
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from paths import APP_NAME, IS_MAC, IS_WIN, glossary_path, output_dir, voice_profile_path

VERSION = "1.0.0"
AUDIO_TYPES = [("녹음 파일", "*.m4a *.mp3 *.wav *.aac *.flac *.ogg *.opus *.mp4 *.mov *.webm *.wma *.amr"), ("모든 파일", "*.*")]
NOTICE = (
    "모두의 회의록은 무료 프로그램입니다.\n\n"
    "• 모든 처리는 이 컴퓨터 안에서만 이뤄지며, 녹음과 텍스트는 어디에도 전송되지 않습니다.\n"
    "• 내가 참여한 대화를 녹음하는 것은 괜찮지만, 내가 빠진 다른 사람들의 대화를 몰래 녹음하면 "
    "통신비밀보호법 위반이 됩니다.\n"
    "• 처음 사용할 때 AI 모델(약 1.6~3GB)을 한 번 내려받습니다."
)


def open_path(p):
    p = str(p)
    if IS_WIN:
        os.startfile(p)
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


class Recorder:
    def __init__(self):
        self.stream = None
        self.wav = None
        self.path = None
        self.level = 0.0
        self.started = 0.0

    def start(self, path: Path):
        import numpy as np
        import sounddevice as sd

        def callback(indata, frames, t, status):
            self.wav.writeframes((np.clip(indata[:, 0], -1, 1) * 32767).astype("<i2").tobytes())
            self.level = float(np.sqrt(np.mean(indata[:, 0] ** 2)))

        last_error = None
        for rate in (16000, 48000, 44100):
            try:
                self.wav = wave.open(str(path), "wb")
                self.wav.setnchannels(1)
                self.wav.setsampwidth(2)
                self.wav.setframerate(rate)
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
        raise RuntimeError(f"마이크를 열 수 없습니다: {last_error}")

    def stop(self) -> Path:
        self.stream.stop()
        self.stream.close()
        self.wav.close()
        self.stream = None
        return self.path

    @property
    def active(self):
        return self.stream is not None


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.engine = None
        self.recorder = Recorder()
        self.voice_recorder = Recorder()
        self.last_output = None

        root.title(f"{APP_NAME} {VERSION}")
        root.minsize(520, 520)
        font = ("Apple SD Gothic Neo", 13) if IS_MAC else ("Malgun Gothic", 10)
        style = ttk.Style()
        style.configure(".", font=font)
        style.configure("Big.TButton", font=(font[0], font[1] + 3, "bold"), padding=10)

        frame = ttk.Frame(root, padding=16)
        frame.pack(fill="both", expand=True)

        top = ttk.Frame(frame)
        top.pack(fill="x")
        ttk.Label(top, text="참석자 수").pack(side="left")
        self.count = ttk.Combobox(top, values=["자동"] + [f"{n}명" for n in range(2, 11)], width=6, state="readonly")
        self.count.current(0)
        self.count.pack(side="left", padx=8)
        ttk.Label(top, text="(알면 정해 주세요. 더 정확해집니다)", foreground="#777").pack(side="left")

        self.rec_btn = ttk.Button(frame, text="●  녹음 시작", style="Big.TButton", command=self.toggle_record)
        self.rec_btn.pack(fill="x", pady=(16, 4))
        rec_info = ttk.Frame(frame)
        rec_info.pack(fill="x")
        self.timer = ttk.Label(rec_info, text="00:00:00")
        self.timer.pack(side="left")
        self.level = ttk.Progressbar(rec_info, maximum=1.0)
        self.level.pack(side="left", fill="x", expand=True, padx=(10, 0))

        ttk.Button(frame, text="녹음 파일 선택해서 텍스트로 만들기…", command=self.pick_files).pack(fill="x", pady=(12, 0))

        tools = ttk.Frame(frame)
        tools.pack(fill="x", pady=12)
        self.voice_btn = ttk.Button(tools, text="내 목소리 등록", command=self.toggle_voice)
        self.voice_btn.pack(side="left", expand=True, fill="x")
        ttk.Button(tools, text="용어집", command=lambda: open_path(glossary_path())).pack(side="left", expand=True, fill="x", padx=6)
        ttk.Button(tools, text="결과 폴더", command=lambda: open_path(output_dir())).pack(side="left", expand=True, fill="x")
        self.voice_state = ttk.Label(frame, foreground="#777")
        self.voice_state.pack(anchor="w")
        self.refresh_voice_state()

        self.progress = ttk.Progressbar(frame, maximum=1.0)
        self.progress.pack(fill="x", pady=(12, 4))
        self.status = ttk.Label(frame, text="준비됨")
        self.status.pack(anchor="w")
        self.logbox = tk.Text(frame, height=10, wrap="word", state="disabled", relief="flat", background="#f6f6f6")
        self.logbox.pack(fill="both", expand=True, pady=(8, 0))
        self.open_btn = ttk.Button(frame, text="결과 열기", command=self.open_last, state="disabled")
        self.open_btn.pack(fill="x", pady=(8, 0))

        threading.Thread(target=self.worker, daemon=True).start()
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(300, self.first_run)

    def first_run(self):
        flag = output_dir() / ".notice_ok"
        if not flag.exists():
            messagebox.showinfo(APP_NAME, NOTICE)
            flag.write_text("ok")

    def log(self, msg=None, progress=None, done=None):
        self.events.put((msg, progress, done))

    def poll(self):
        try:
            while True:
                msg, progress, done = self.events.get_nowait()
                if msg:
                    self.status.config(text=msg)
                    if not msg.startswith("받아쓰기 ") and "내려받는 중" not in msg:
                        self.append(msg)
                if progress is not None:
                    self.progress["value"] = progress
                if done:
                    self.last_output = done
                    self.open_btn.config(state="normal")
        except queue.Empty:
            pass
        for rec in (self.recorder, self.voice_recorder):
            if rec.active:
                self.level["value"] = min(rec.level * 8, 1.0)
                el = int(time.time() - rec.started)
                self.timer.config(text=f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}")
        self.root.after(100, self.poll)

    def append(self, msg):
        self.logbox.config(state="normal")
        self.logbox.insert("end", f"{datetime.now():%H:%M} {msg}\n")
        self.logbox.see("end")
        self.logbox.config(state="disabled")

    def get_engine(self):
        if self.engine is None:
            self.log("AI 엔진 불러오는 중…")
            from engine import Engine
            self.engine = Engine(self.log)
        return self.engine

    def worker(self):
        while True:
            kind, path, count = self.jobs.get()
            try:
                if kind == "voice":
                    self.get_engine().register_voice(path)
                    self.root.after(0, self.refresh_voice_state)
                else:
                    self.get_engine().process(path, count)
            except Exception as e:
                traceback.print_exc()
                self.log(f"오류: {e}", progress=0)
            if self.jobs.empty():
                self.log("대기 중 — 다른 작업이 없습니다")

    def selected_count(self):
        v = self.count.get()
        return None if v == "자동" else int(v.rstrip("명"))

    def toggle_record(self):
        if self.voice_recorder.active:
            return
        if self.recorder.active:
            path = self.recorder.stop()
            self.rec_btn.config(text="●  녹음 시작")
            self.level["value"] = 0
            self.append(f"녹음 저장: {path.name}")
            self.jobs.put(("meeting", path, self.selected_count()))
            return
        path = output_dir() / "녹음" / f"회의_{datetime.now():%Y%m%d_%H%M%S}.wav"
        try:
            self.recorder.start(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"{e}\n\n시스템 설정에서 이 프로그램의 마이크 사용을 허용해 주세요.")
            return
        self.rec_btn.config(text="■  녹음 정지 (정지하면 바로 텍스트로 만듭니다)")
        self.append("녹음 시작")

    def pick_files(self):
        files = filedialog.askopenfilenames(title="녹음 파일 선택", filetypes=AUDIO_TYPES)
        for f in files:
            self.append(f"대기열 추가: {Path(f).name}")
            self.jobs.put(("meeting", Path(f), self.selected_count()))

    def toggle_voice(self):
        if self.recorder.active:
            return
        if self.voice_recorder.active:
            path = self.voice_recorder.stop()
            self.voice_btn.config(text="내 목소리 등록")
            self.level["value"] = 0
            self.jobs.put(("voice", path, None))
            return
        if not messagebox.askokcancel(
            APP_NAME,
            "혼자 있는 조용한 곳에서 평소 말투로 20초 이상 말한 뒤 '등록 끝내기'를 누르세요.\n"
            "(예: 자기소개, 오늘 한 일)\n\n등록하면 결과에서 내 말은 [나]로 표시됩니다.",
        ):
            return
        path = output_dir() / "녹음" / "내목소리.wav"
        try:
            self.voice_recorder.start(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, str(e))
            return
        self.voice_btn.config(text="■ 등록 끝내기")

    def refresh_voice_state(self):
        self.voice_state.config(
            text="내 목소리: 등록됨 → 결과에 [나]로 표시" if voice_profile_path().exists() else "내 목소리: 등록 안 됨 (등록하면 내 말이 [나]로 표시됩니다)"
        )

    def open_last(self):
        if self.last_output:
            open_path(self.last_output)

    def on_close(self):
        if self.recorder.active:
            if not messagebox.askyesno(APP_NAME, "녹음 중입니다. 녹음을 저장하고 종료할까요?"):
                return
            self.recorder.stop()
        if not self.jobs.empty() or (self.engine and self.engine.lock.locked()):
            if not messagebox.askyesno(APP_NAME, "처리 중인 작업이 있습니다. 종료하면 중단됩니다. 종료할까요?"):
                return
        self.root.destroy()
        os._exit(0)


def main():
    multiprocessing.freeze_support()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
