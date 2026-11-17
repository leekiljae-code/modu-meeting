import os
import platform
import re
import threading
import time
from datetime import datetime
from pathlib import Path

import numpy as np

from paths import IS_MAC, IS_WIN, glossary_path, models_dir, output_dir, resource_dir, voice_profile_path

os.environ.setdefault("PYANNOTE_METRICS_ENABLED", "0")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

SR = 16000
MERGE_SIM = 0.8
SHORT_SPK_SEC = 4.0
SHORT_MERGE_SIM = 0.5
MY_VOICE_SIM = 0.6
BLIP_SEC = 0.8
JOIN_GAP = 2.0
LOW_CONF = 0.35
NUM_CONF = 0.7
HALLUCINATIONS = (
    "시청해주셔서", "시청해 주셔서", "다음 영상에서", "구독과 좋아요", "구독 좋아요", "좋아요와 구독",
    "MBC 뉴스", "KBS 뉴스", "SBS 뉴스", "자막 제공", "자막 by", "한글자막", "감사합니다 감사합니다",
)

WHISPER_REPOS = {
    "mlx": "mlx-community/whisper-large-v3-mlx",
    "gpu": "Systran/faster-whisper-large-v3",
    "cpu": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
}


def load_audio(path) -> np.ndarray:
    import av
    chunks = []
    with av.open(str(path)) as container:
        stream = container.streams.audio[0]
        resampler = av.audio.resampler.AudioResampler(format="flt", layout="mono", rate=SR)
        for frame in container.decode(stream):
            for out in resampler.resample(frame):
                chunks.append(out.to_ndarray().reshape(-1))
        for out in resampler.resample(None):
            chunks.append(out.to_ndarray().reshape(-1))
    if not chunks:
        raise ValueError("소리가 없는 파일입니다.")
    return np.concatenate(chunks).astype(np.float32)


def normalize_loudness(audio: np.ndarray) -> np.ndarray:
    win = SR // 2
    n = int(np.ceil(len(audio) / win))
    padded = np.pad(audio, (0, n * win - len(audio)))
    rms = np.sqrt(np.mean(padded.reshape(n, win) ** 2, axis=1) + 1e-10)
    kernel = np.exp(-0.5 * (np.arange(-15, 16) / 5.0) ** 2)
    kernel /= kernel.sum()
    smooth = np.convolve(np.pad(rms, 15, mode="edge"), kernel, mode="valid")
    gain = np.clip(0.1 / np.maximum(smooth, 1e-4), 0.5, 20.0)
    gain_curve = np.interp(np.arange(len(audio)), np.arange(n) * win + win / 2, gain)
    return np.clip(audio * gain_curve, -1.0, 1.0).astype(np.float32)


def fmt_time(sec: float) -> str:
    s = int(sec)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def cosine(a, b) -> float:
    return float(np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b) + 1e-10))


def read_glossary():
    hints, fixes = [], []
    for line in glossary_path().read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=>" in line:
            wrong, right = (x.strip() for x in line.split("=>", 1))
            if wrong:
                fixes.append((wrong, right))
        else:
            hints += [w.strip() for w in line.split(",") if w.strip()]
    return hints, fixes


def folder_size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


class Engine:
    def __init__(self, log):
        self.log = log
        self.pipeline = None
        self.asr = None
        self.backend = None
        self.lock = threading.Lock()

    def _download(self, repo: str, label: str, approx_mb: int) -> Path:
        from huggingface_hub import snapshot_download
        target = models_dir() / repo.replace("/", "__")
        if (target / ".complete").exists():
            return target
        target.mkdir(parents=True, exist_ok=True)
        done = threading.Event()

        def report():
            while not done.wait(2):
                mb = folder_size(target) / 1e6
                self.log(f"{label} 내려받는 중 {mb:,.0f} / 약 {approx_mb:,} MB", progress=min(mb / approx_mb, 0.99))

        t = threading.Thread(target=report, daemon=True)
        t.start()
        try:
            snapshot_download(repo, local_dir=str(target))
        finally:
            done.set()
        (target / ".complete").write_text("ok")
        return target

    def _torch_device(self):
        import torch
        if torch.cuda.is_available():
            return torch.device("cuda")
        if IS_MAC and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")

    def load_diarizer(self):
        if self.pipeline is not None:
            return self.pipeline
        self.log("화자 분리 모델 준비 중…")
        import torch
        from pyannote.audio import Pipeline
        pipe = Pipeline.from_pretrained(str(resource_dir() / "models" / "pyannote-community-1"))
        params = pipe.parameters(instantiated=True)
        params["clustering"]["Fa"] = 0.15
        pipe.instantiate(params)
        device = self._torch_device()
        try:
            pipe.to(device)
        except Exception:
            pipe.to(torch.device("cpu"))
        self.pipeline = pipe
        return pipe

    def _choose_backend(self) -> str:
        if IS_MAC and platform.machine() == "arm64":
            try:
                import mlx_whisper  # noqa: F401
                return "mlx"
            except Exception:
                pass
        if IS_WIN:
            try:
                from cuda_setup import prepare_cuda
                if prepare_cuda(self.log):
                    return "gpu"
            except Exception as e:
                self.log(f"그래픽카드 가속 준비 실패, CPU로 진행합니다 ({e})")
        else:
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    return "gpu"
            except Exception:
                pass
        return "cpu"

    def load_asr(self):
        if self.asr is not None:
            return
        self.backend = self._choose_backend()
        size = {"mlx": 3100, "gpu": 3100, "cpu": 1650}[self.backend]
        model_path = self._download(WHISPER_REPOS[self.backend], "받아쓰기 모델", size)
        self.log("받아쓰기 모델 준비 중…")
        if self.backend == "mlx":
            self.asr = str(model_path)
        else:
            from faster_whisper import WhisperModel
            if self.backend == "gpu":
                self.asr = WhisperModel(str(model_path), device="cuda", compute_type="float16")
            else:
                threads = max(1, (os.cpu_count() or 4) - 1)
                self.asr = WhisperModel(str(model_path), device="cpu", compute_type="int8", cpu_threads=threads)

    def embed(self, audio: np.ndarray) -> np.ndarray:
        import torch
        pipe = self.load_diarizer()
        win = SR * 10
        pieces = [audio[i:i + win] for i in range(0, len(audio), win)]
        pieces = [p for p in pieces if len(p) >= SR * 3 and np.sqrt(np.mean(p ** 2)) > 0.003]
        if not pieces:
            raise ValueError("목소리가 너무 짧거나 작습니다. 10초 이상 또렷하게 말해 주세요.")
        embs = []
        for p in pieces:
            wav = torch.from_numpy(normalize_loudness(p))[None, None]
            e = np.asarray(pipe._embedding(wav)).reshape(-1)
            if np.all(np.isfinite(e)):
                embs.append(e / np.linalg.norm(e))
        if not embs:
            raise ValueError("목소리를 인식하지 못했습니다. 다시 녹음해 주세요.")
        return np.mean(embs, axis=0)

    def register_voice(self, path) -> None:
        with self.lock:
            self.log("내 목소리 분석 중…")
            emb = self.embed(load_audio(path))
            np.save(voice_profile_path(), emb)
            self.log("내 목소리 등록 완료")

    def _diarize(self, audio, num_speakers):
        import torch
        pipe = self.load_diarizer()
        self.log("화자 분리 중…", progress=0.0)
        steps = {"segmentation": (0.0, 0.3), "embeddings": (0.3, 0.95)}

        def hook(step, artefact, file=None, completed=None, total=None):
            if step in steps and completed is not None and total:
                a, b = steps[step]
                self.log(None, progress=a + (b - a) * completed / total)

        kwargs = {"num_speakers": num_speakers} if num_speakers else {"min_speakers": 1, "max_speakers": 8}
        wav = torch.from_numpy(normalize_loudness(audio))[None]
        out = pipe({"waveform": wav, "sample_rate": SR}, hook=hook, **kwargs)
        labels = out.speaker_diarization.labels()
        emb = {lab: out.speaker_embeddings[i] for i, lab in enumerate(labels)}
        segs = [(t.start, t.end, lab) for t, _, lab in out.exclusive_speaker_diarization.itertracks(yield_label=True)]
        return segs, emb

    def _merge_speakers(self, segs, emb, fixed_count):
        dur = {}
        for s, e, lab in segs:
            dur[lab] = dur.get(lab, 0.0) + e - s
        alias = {lab: lab for lab in dur}

        def root(x):
            while alias[x] != x:
                x = alias[x]
            return x

        def merge(a, b):
            ra, rb = root(a), root(b)
            if ra == rb:
                return
            keep, drop = (ra, rb) if dur[ra] >= dur[rb] else (rb, ra)
            w1, w2 = dur[keep], dur[drop]
            emb[keep] = (emb[keep] * w1 + emb[drop] * w2) / (w1 + w2)
            dur[keep] += dur[drop]
            alias[drop] = keep

        valid = [l for l in dur if l in emb and np.all(np.isfinite(emb[l]))]
        if not fixed_count:
            changed = True
            while changed:
                changed = False
                roots = sorted({root(l) for l in valid})
                best = None
                for i, a in enumerate(roots):
                    for b in roots[i + 1:]:
                        sim = cosine(emb[a], emb[b])
                        if sim >= MERGE_SIM and (best is None or sim > best[0]):
                            best = (sim, a, b)
                if best:
                    merge(best[1], best[2])
                    changed = True
            for lab in sorted({root(l) for l in valid}, key=lambda l: dur[l]):
                if dur[lab] >= SHORT_SPK_SEC:
                    continue
                others = [o for o in {root(l) for l in valid} if o != lab]
                if not others:
                    continue
                sim, target = max((cosine(emb[lab], emb[o]), o) for o in others)
                if sim >= SHORT_MERGE_SIM:
                    merge(target, lab)
        return [(s, e, root(lab) if lab in alias else lab) for s, e, lab in segs], {root(l): emb[root(l)] for l in valid}

    def _clean_segments(self, segs):
        segs = sorted(segs)
        cleaned = []
        for i, (s, e, lab) in enumerate(segs):
            if e - s < BLIP_SEC and 0 < i < len(segs) - 1 and segs[i - 1][2] == segs[i + 1][2] != lab:
                continue
            cleaned.append([s, e, lab])
        joined = []
        for s, e, lab in cleaned:
            if joined and joined[-1][2] == lab and s - joined[-1][1] <= JOIN_GAP:
                joined[-1][1] = max(joined[-1][1], e)
            else:
                joined.append([s, e, lab])
        return [j for j in joined if j[1] - j[0] >= 0.3]

    def _name_speakers(self, segs, emb):
        names = {}
        if voice_profile_path().exists() and emb:
            mine = np.load(voice_profile_path())
            names = {lab: "나" for lab, v in emb.items() if cosine(mine, v) >= MY_VOICE_SIM}
        n = 0
        for _, _, lab in sorted(segs):
            if lab not in names:
                n += 1
                names[lab] = f"화자{n}"
        return [(s, e, names[lab]) for s, e, lab in segs]

    def _transcribe_piece(self, audio, prompt, fast=False):
        common = dict(language="ko", initial_prompt=prompt or None, word_timestamps=True,
                      condition_on_previous_text=False, compression_ratio_threshold=2.4,
                      no_speech_threshold=0.6, hallucination_silence_threshold=2.0)
        results = []
        if self.backend == "mlx":
            import mlx_whisper
            r = mlx_whisper.transcribe(audio, path_or_hf_repo=self.asr, logprob_threshold=-1.0, verbose=None, **common)
            for seg in r.get("segments", []):
                results.append((seg["text"], [w.get("probability", 1.0) for w in seg.get("words", [])],
                                [w.get("word", "") for w in seg.get("words", [])],
                                seg.get("compression_ratio", 0), seg.get("no_speech_prob", 0), seg.get("avg_logprob", 0)))
        else:
            segments, _ = self.asr.transcribe(audio, log_prob_threshold=-1.0, beam_size=1 if fast else 5, vad_filter=False, **common)
            for seg in segments:
                words = seg.words or []
                results.append((seg.text, [w.probability for w in words], [w.word for w in words],
                                seg.compression_ratio, seg.no_speech_prob, seg.avg_logprob))
        return results

    def _render_text(self, results, fixes):
        parts = []
        for text, probs, words, cr, nsp, lp in results:
            if cr > 2.4 or (nsp > 0.6 and lp < -1.0):
                continue
            if any(h in text for h in HALLUCINATIONS):
                continue
            if probs and float(np.mean(probs)) < LOW_CONF:
                parts.append("(잘 안 들림)")
                continue
            if words:
                text = "".join(w + "(?)" if re.search(r"\d", w) and p < NUM_CONF else w for w, p in zip(words, probs))
            parts.append(text.strip())
        line = re.sub(r"(\(잘 안 들림\)\s*)+", "(잘 안 들림) ", " ".join(p for p in parts if p)).strip()
        for wrong, right in fixes:
            line = line.replace(wrong, right)
        return line

    def process(self, path, num_speakers=None, title=None) -> Path:
        with self.lock:
            path = Path(path)
            started = time.time()
            self.log(f"처리 시작: {path.name}", progress=0.0)
            audio = load_audio(path)
            minutes = len(audio) / SR / 60
            self.log(f"길이 {minutes:.1f}분")
            segs, emb = self._diarize(audio, num_speakers)
            segs, emb = self._merge_speakers(segs, emb, bool(num_speakers))
            segs = self._clean_segments(self._name_speakers(segs, emb))
            self.load_asr()
            hints, fixes = read_glossary()
            lines, recent = [], []
            total = len(segs)
            for i, (s, e, name) in enumerate(segs):
                self.log(f"받아쓰기 {i + 1}/{total}", progress=(i + 1) / max(total, 1))
                piece = audio[int(s * SR):int(e * SR)]
                prompt = (", ".join(hints) + " " + " ".join(recent[-3:])).strip()[-220:]
                text = self._render_text(self._transcribe_piece(piece, prompt), fixes)
                if not text:
                    continue
                lines.append(f"[{fmt_time(s)}] [{name}] {text}")
                recent.append(text)
            speakers = sorted({n for _, _, n in segs}, key=lambda n: (n != "나", n))
            stamp = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d_%H%M")
            out = output_dir() / f"{stamp}_{title or path.stem}.txt"
            header = [
                f"# 원본: {path.name}",
                f"# 길이: {minutes:.0f}분 | 참석자: {len(speakers)}명 ({', '.join(speakers)})",
                "# (잘 안 들림) = 알아듣기 어려운 구간, 숫자(?) = 확신이 낮은 숫자",
                "",
            ]
            out.write_text("\n".join(header + lines) + "\n", encoding="utf-8")
            self.log(f"완료 ({(time.time() - started) / 60:.1f}분 걸림): {out.name}", progress=1.0, done=out)
            return out



class LiveTranscriber:
    FRAME = SR * 30 // 1000
    END_SILENCE = 0.35
    MAX_UTTER = 15.0
    MIN_UTTER = 0.8
    MATCH_SIM = 0.5
    NEW_SPEAKER_MIN = 3.0
    MAX_LAG = 45.0

    def __init__(self, engine: Engine, on_lines, on_status, num_speakers=None):
        self.engine = engine
        self.on_lines = on_lines
        self.on_status = on_status
        self.max_speakers = num_speakers
        self.incoming = []
        self.cond = threading.Condition()
        self.stop_event = threading.Event()
        self.centroids = {}
        self.last_name = None
        self.mine = np.load(voice_profile_path()) if voice_profile_path().exists() else None
        self.recent = []
        self.fed = 0.0
        threading.Thread(target=self._run, daemon=True).start()

    def feed(self, samples: np.ndarray, rate: int):
        if rate != SR:
            n = int(len(samples) * SR / rate)
            samples = np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples)
        with self.cond:
            self.incoming.append(samples.astype(np.float32))
            self.fed += len(samples) / SR
            self.cond.notify()

    def stop(self):
        self.stop_event.set()
        with self.cond:
            self.cond.notify()

    def _pull(self):
        with self.cond:
            while not self.incoming and not self.stop_event.is_set():
                self.cond.wait(0.5)
            data, self.incoming = self.incoming, []
        return np.concatenate(data) if data else None

    def _utterances(self):
        pend = np.zeros(0, np.float32)
        t0 = 0.0
        pos = 0
        floor = 0.003
        start = None
        last = None
        silence_end = int(self.END_SILENCE * SR / self.FRAME)
        while not self.stop_event.is_set():
            new = self._pull()
            if new is None:
                continue
            pend = np.concatenate([pend, new])
            while pos + self.FRAME <= len(pend):
                frame = pend[pos:pos + self.FRAME]
                rms = float(np.sqrt(np.mean(frame ** 2)))
                speech = rms > max(0.006, floor * 3)
                if not speech:
                    floor = floor * 0.97 + rms * 0.03
                idx = pos // self.FRAME
                if speech:
                    start = idx if start is None else start
                    last = idx
                pos += self.FRAME
                if start is None:
                    continue
                long_enough = (idx - start) * self.FRAME >= self.MAX_UTTER * SR
                if idx - last >= silence_end or long_enough:
                    a = max(0, start * self.FRAME - SR // 5)
                    b = min(len(pend), (last + 1) * self.FRAME + SR // 5)
                    yield t0 + a / SR, pend[a:b].copy()
                    cut = b if long_enough else pos
                    pend, t0, pos = pend[cut:], t0 + cut / SR, pos - cut
                    pos -= pos % self.FRAME
                    start = last = None
            if start is None and len(pend) > SR * 2:
                cut = len(pend) - SR // 2
                cut -= cut % self.FRAME
                pend, t0, pos = pend[cut:], t0 + cut / SR, max(0, pos - cut)

    def _name_for(self, emb, duration):
        if emb is None or not np.all(np.isfinite(emb)):
            return self.last_name or "화자1"
        known = dict(self.centroids)
        if self.mine is not None:
            known["나"] = self.mine
        others = self.max_speakers - (1 if self.mine is not None else 0) if self.max_speakers else None
        if known:
            sim, name = max((cosine(c, emb), n) for n, c in known.items())
            full = others is not None and len(self.centroids) >= others
            if sim >= self.MATCH_SIM or full or duration < self.NEW_SPEAKER_MIN:
                if name != "나":
                    self.centroids[name] = self.centroids[name] * 0.85 + emb * 0.15
                return name
        name = f"화자{len(self.centroids) + 1}"
        self.centroids[name] = emb
        return name

    def _run(self):
        import torch
        eng = self.engine
        try:
            with eng.lock:
                eng.load_diarizer()
                eng.load_asr()
            self.on_status("듣는 중")
        except Exception as e:
            self.on_status(f"실시간 받아쓰기를 사용할 수 없습니다 ({e})")
            return
        hints, fixes = read_glossary()
        for start, audio in self._utterances():
            if len(audio) < self.MIN_UTTER * SR:
                continue
            if self.fed - (start + len(audio) / SR) > self.MAX_LAG:
                self.on_status("처리가 밀려 일부 구간을 건너뜁니다")
                continue
            with eng.lock:
                if self.stop_event.is_set():
                    return
                try:
                    emb = None
                    if len(audio) >= SR:
                        e = np.asarray(eng.pipeline._embedding(torch.from_numpy(normalize_loudness(audio))[None, None])).reshape(-1)
                        emb = e / (np.linalg.norm(e) + 1e-10)
                    name = self._name_for(emb, len(audio) / SR)
                    prompt = (", ".join(hints) + " " + " ".join(self.recent[-3:])).strip()[-220:]
                    text = eng._render_text(eng._transcribe_piece(audio, prompt, fast=True), fixes)
                except Exception as e:
                    self.on_status(f"실시간 받아쓰기 오류: {e}")
                    continue
            if text:
                self.last_name = name
                self.recent.append(text)
                self.on_lines([{"t": fmt_time(start), "who": name, "text": text}])
