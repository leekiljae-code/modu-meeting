import json
import os
import urllib.request
import zipfile
from pathlib import Path

from paths import data_dir

WHEELS = {
    "nvidia-cublas-cu12": "12.4.5.8",
    "nvidia-cudnn-cu12": "9.1.0.70",
}


def _cuda_dir() -> Path:
    return data_dir() / "cuda"


def _activate(folder: Path) -> None:
    os.add_dll_directory(str(folder))
    os.environ["PATH"] = str(folder) + os.pathsep + os.environ.get("PATH", "")


def _wheel_url(name: str, version: str) -> str:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=30) as r:
        info = json.load(r)
    for f in info["urls"]:
        if f["filename"].endswith("win_amd64.whl"):
            return f["url"]
    raise RuntimeError(f"{name} 윈도우 파일을 찾지 못했습니다")


def _download_dlls(log, folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for i, (name, version) in enumerate(WHEELS.items(), 1):
        tmp = folder / f"{name}.whl"
        url = _wheel_url(name, version)
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            total = int(r.headers.get("Content-Length", 0)) or 1
            got = 0
            while chunk := r.read(1 << 20):
                f.write(chunk)
                got += len(chunk)
                log(f"그래픽카드 가속 파일 내려받는 중 ({i}/{len(WHEELS)})… {got / 1e6:,.0f} / {total / 1e6:,.0f} MB", progress=got / total)
        with zipfile.ZipFile(tmp) as z:
            for member in z.namelist():
                if member.lower().endswith(".dll"):
                    (folder / Path(member).name).write_bytes(z.read(member))
        tmp.unlink()
    (folder / ".complete").write_text("ok")


def prepare_cuda(log) -> bool:
    import ctranslate2
    if ctranslate2.get_cuda_device_count() == 0:
        return False
    folder = _cuda_dir()
    if not (folder / ".complete").exists():
        log("NVIDIA 그래픽카드 발견 — 가속 파일을 한 번만 내려받습니다 (약 1GB)")
        _download_dlls(log, folder)
    _activate(folder)
    return True
