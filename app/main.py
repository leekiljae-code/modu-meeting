import multiprocessing
import os
import queue
import subprocess
import threading
import time
import traceback
import wave
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox

import customtkinter as ctk

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
FONT = "Apple SD Gothic Neo" if IS_MAC else ("Malgun Gothic" if IS_WIN else "NanumGothic")
ACCENT = ("#0f8f88", "#14a39b")
ACCENT_HOVER = ("#0b7570", "#0f8f88")
REC = ("#e5484d", "#e5484d")
REC_HOVER = ("#c9373c", "#c9373c")
CARD = ("#ffffff", "#1f2124")
BG = ("#f3f5f7", "#141517")
MUTED = ("#6b7280", "#9ca3af")
SUBTLE = ("#eef1f4", "#2a2d31")
COUNTS = ["자동", "2", "3", "4", "5", "6", "7", "8"]


def open_path(p):
    p = str(p)
    if IS_WIN:
        os.startfile(p)
    elif IS_MAC:
        subprocess.Popen(["open", p])
    else:
        subprocess.Popen(["xdg-open", p])


def font(size, weight="normal"):
    return ctk.CTkFont(family=FONT, size=size, weight=weight)


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
    def __init__(self, root: ctk.CTk):
        self.root = root
        self.events = queue.Queue()
        self.jobs = queue.Queue()
        self.engine = None
        self.recorder = Recorder()
        self.voice_recorder = Recorder()
        self.last_output = None

        root.title(f"{APP_NAME} {VERSION}")
        root.geometry("560x760")
        root.minsize(480, 640)
        root.configure(fg_color=BG)

        page = ctk.CTkFrame(root, fg_color="transparent")
        page.pack(fill="both", expand=True, padx=22, pady=20)

        head = ctk.CTkFrame(page, fg_color="transparent")
        head.pack(fill="x")
        ctk.CTkLabel(head, text=APP_NAME, font=font(24, "bold")).pack(anchor="w")
        ctk.CTkLabel(head, text="말한 사람별로 정리되는 무료 회의록 · 내 컴퓨터 안에서만 처리", font=font(13), text_color=MUTED).pack(anchor="w")

        rec_card = self.card(page, pady=(16, 0))
        ctk.CTkLabel(rec_card, text="참석자 수", font=font(13, "bold")).pack(anchor="w")
        ctk.CTkLabel(rec_card, text="알면 골라 주세요. 더 정확해집니다", font=font(12), text_color=MUTED).pack(anchor="w")
        self.count = ctk.CTkSegmentedButton(
            rec_card, values=COUNTS, font=font(13), height=34,
            fg_color=SUBTLE, unselected_color=SUBTLE, unselected_hover_color=("#e2e6ea", "#34383d"),
            selected_color=ACCENT, selected_hover_color=ACCENT_HOVER, text_color=("#111827", "#e5e7eb"),
        )
        self.count.set("자동")
        self.count.pack(fill="x", pady=(8, 18))

        self.timer = ctk.CTkLabel(rec_card, text="00:00:00", font=ctk.CTkFont(family="Consolas" if IS_WIN else FONT, size=40, weight="bold"))
        self.timer.pack()
        self.level = ctk.CTkProgressBar(rec_card, height=6, fg_color=SUBTLE, progress_color=REC)
        self.level.set(0)
        self.level.pack(fill="x", padx=40, pady=(4, 16))
        self.rec_btn = ctk.CTkButton(
            rec_card, text="●   녹음 시작", font=font(17, "bold"), height=54, corner_radius=27,
            fg_color=REC, hover_color=REC_HOVER, command=self.toggle_record,
        )
        self.rec_btn.pack(fill="x")
        ctk.CTkButton(
            rec_card, text="녹음 파일 불러오기  (스마트폰 녹음 등)", font=font(14), height=42, corner_radius=21,
            fg_color="transparent", border_width=1, border_color=SUBTLE, text_color=("#111827", "#e5e7eb"),
            hover_color=SUBTLE, command=self.pick_files,
        ).pack(fill="x", pady=(10, 0))

        tools = ctk.CTkFrame(page, fg_color="transparent")
        tools.pack(fill="x", pady=(12, 0))
        tools.grid_columnconfigure((0, 1, 2), weight=1, uniform="t")
        self.voice_btn = self.tool_button(tools, "내 목소리 등록", self.toggle_voice, 0)
        self.tool_button(tools, "용어집", lambda: open_path(glossary_path()), 1)
        self.tool_button(tools, "결과 폴더", lambda: open_path(output_dir()), 2)
        self.voice_state = ctk.CTkLabel(page, font=font(12), text_color=MUTED)
        self.voice_state.pack(anchor="w", pady=(6, 0))
        self.refresh_voice_state()

        status_card = self.card(page, pady=(10, 0), expand=True)
        self.status = ctk.CTkLabel(status_card, text="준비됨", font=font(14, "bold"), anchor="w")
        self.status.pack(fill="x")
        self.progress = ctk.CTkProgressBar(status_card, height=8, fg_color=SUBTLE, progress_color=ACCENT)
        self.progress.set(0)
        self.progress.pack(fill="x", pady=(8, 10))
        self.open_btn = ctk.CTkButton(
            status_card, text="결과 열기", font=font(14, "bold"), height=42, corner_radius=21,
            fg_color=ACCENT, hover_color=ACCENT_HOVER, state="disabled", command=self.open_last,
        )
        self.open_btn.pack(side="bottom", fill="x", pady=(10, 0))
        self.logbox = ctk.CTkTextbox(status_card, height=90, font=font(12), fg_color=SUBTLE, corner_radius=10, wrap="word", state="disabled")
        self.logbox.pack(fill="both", expand=True)

        threading.Thread(target=self.worker, daemon=True).start()
        root.after(100, self.poll)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(300, self.first_run)

    def card(self, parent, pady, expand=False):
        outer = ctk.CTkFrame(parent, fg_color=CARD, corner_radius=16)
        outer.pack(fill="both" if expand else "x", expand=expand, pady=pady)
        inner = ctk.CTkFrame(outer, fg_color="transparent")
        inner.pack(fill="both", expand=True, padx=18, pady=16)
        return inner

    def tool_button(self, parent, text, command, col):
        b = ctk.CTkButton(
            parent, text=text, font=font(13), height=38, corner_radius=12,
            fg_color=CARD, hover_color=SUBTLE, text_color=("#111827", "#e5e7eb"), command=command,
        )
        b.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 4, 0 if col == 2 else 4))
        return b

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
                    self.status.configure(text=msg)
                    if not msg.startswith("받아쓰기 ") and "내려받는 중" not in msg:
                        self.append(msg)
                if progress is not None:
                    self.progress.set(progress)
                if done:
                    self.last_output = done
                    self.open_btn.configure(state="normal")
        except queue.Empty:
            pass
        for rec in (self.recorder, self.voice_recorder):
            if rec.active:
                self.level.set(min(rec.level * 8, 1.0))
                el = int(time.time() - rec.started)
                self.timer.configure(text=f"{el // 3600:02d}:{el % 3600 // 60:02d}:{el % 60:02d}")
        self.root.after(100, self.poll)

    def append(self, msg):
        self.logbox.configure(state="normal")
        self.logbox.insert("end", f"{datetime.now():%H:%M}  {msg}\n")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

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
        return None if v == "자동" else int(v)

    def toggle_record(self):
        if self.voice_recorder.active:
            return
        if self.recorder.active:
            path = self.recorder.stop()
            self.rec_btn.configure(text="●   녹음 시작", fg_color=REC, hover_color=REC_HOVER)
            self.level.set(0)
            self.append(f"녹음 저장: {path.name}")
            self.jobs.put(("meeting", path, self.selected_count()))
            return
        path = output_dir() / "녹음" / f"회의_{datetime.now():%Y%m%d_%H%M%S}.wav"
        try:
            self.recorder.start(path)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"{e}\n\n시스템 설정에서 이 프로그램의 마이크 사용을 허용해 주세요.")
            return
        self.rec_btn.configure(text="■   녹음 정지  ·  정지하면 바로 텍스트로", fg_color=("#111827", "#374151"), hover_color=("#000000", "#4b5563"))
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
            self.voice_btn.configure(text="내 목소리 등록", fg_color=CARD, text_color=("#111827", "#e5e7eb"))
            self.level.set(0)
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
        self.voice_btn.configure(text="■ 등록 끝내기", fg_color=REC, text_color="#ffffff")

    def refresh_voice_state(self):
        self.voice_state.configure(
            text="✓ 내 목소리 등록됨 — 결과에 [나]로 표시됩니다" if voice_profile_path().exists() else "내 목소리를 등록하면 내 말이 [나]로 표시됩니다"
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
    ctk.set_appearance_mode("system")
    root = ctk.CTk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
