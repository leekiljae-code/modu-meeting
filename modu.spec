import sys
from PyInstaller.utils.hooks import collect_all, copy_metadata

IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"

packages = [
    "pyannote.audio", "pyannote.core", "pyannote.pipeline", "pyannote.database", "pyannote.metrics",
    "lightning", "lightning_fabric", "pytorch_lightning", "torchmetrics", "lightning_utilities",
    "asteroid_filterbanks", "torch_audiomentations", "pytorch_metric_learning", "torchaudio",
    "faster_whisper", "ctranslate2", "av", "sounddevice", "_sounddevice_data", "tokenizers", "huggingface_hub",
    "einops", "safetensors", "omegaconf", "julius", "primePy", "torch_pitch_shift", "opentelemetry",
]
if IS_MAC:
    packages += ["mlx", "mlx_whisper", "tiktoken", "tiktoken_ext", "numba", "llvmlite"]

datas, binaries, hiddenimports = [("models", "models")], [], ["engine", "cuda_setup"]
for pkg in packages:
    try:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
    except Exception as e:
        print(f"skip {pkg}: {e}")

for dist in [
    "pyannote.audio", "pyannote.core", "pyannote.pipeline", "pyannote.database", "pyannote.metrics",
    "torch", "torchaudio", "lightning", "pytorch_lightning", "lightning_fabric", "torchmetrics", "lightning_utilities",
    "huggingface_hub", "tokenizers", "faster_whisper", "ctranslate2", "numpy", "scipy", "tqdm", "regex",
    "requests", "packaging", "filelock", "safetensors", "asteroid_filterbanks", "pytorch_metric_learning",
    "torch_audiomentations", "opentelemetry-api", "opentelemetry-sdk", "torchcodec", "mlx", "mlx_whisper",
]:
    try:
        datas += copy_metadata(dist)
    except Exception:
        pass

a = Analysis(
    ["app/main.py"],
    pathex=["app"],
    datas=datas,
    binaries=binaries,
    hiddenimports=hiddenimports,
    excludes=["matplotlib", "IPython", "jupyter", "notebook", "pytest", "tensorboard", "torchcodec"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ModuMeeting",
    console=False,
    icon="assets/icon.ico" if IS_WIN else ("assets/icon.icns" if IS_MAC else None),
)
coll = COLLECT(exe, a.binaries, a.datas, name="ModuMeeting")

if IS_MAC:
    app = BUNDLE(
        coll,
        name="모두의 회의록.app",
        icon="assets/icon.icns",
        bundle_identifier="kr.thevr.modumeeting",
        info_plist={
            "CFBundleDisplayName": "모두의 회의록",
            "CFBundleShortVersionString": "1.0.0",
            "NSMicrophoneUsageDescription": "회의를 녹음해 화자별 텍스트로 만들기 위해 마이크가 필요합니다.",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "13.0",
        },
    )
