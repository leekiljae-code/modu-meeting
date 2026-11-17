import os
import sys
from pathlib import Path

APP_NAME = "모두의 회의록"
APP_ID = "ModuMeeting"
IS_MAC = sys.platform == "darwin"
IS_WIN = sys.platform == "win32"


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    if IS_MAC:
        base = Path.home() / "Library" / "Application Support"
    elif IS_WIN:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    d = base / APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


def models_dir() -> Path:
    d = data_dir() / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


def documents_dir() -> Path:
    if IS_WIN:
        import ctypes
        import uuid
        buf = ctypes.c_wchar_p()
        folder_id = ctypes.create_string_buffer(uuid.UUID("FDD39AD0-238F-46AF-ADB4-6C85480369C7").bytes_le)
        try:
            if ctypes.windll.shell32.SHGetKnownFolderPath(folder_id, 0, None, ctypes.byref(buf)) == 0:
                path = buf.value
                ctypes.windll.ole32.CoTaskMemFree(buf)
                return Path(path)
        except OSError:
            pass
    return Path.home() / "Documents"


def output_dir() -> Path:
    legacy = Path.home() / "Documents" / "모두의회의록"
    d = legacy if legacy.exists() else documents_dir() / "모두의회의록"
    (d / "녹음").mkdir(parents=True, exist_ok=True)
    return d


def glossary_path() -> Path:
    p = output_dir() / "용어.txt"
    if not p.exists():
        p.write_text(
            "# 받아쓰기 힌트 단어는 쉼표로 구분해 한 줄에 적으세요.\n"
            "# 자주 틀리는 말은  틀린말 => 바른말  형식으로 적으면 자동 교정됩니다.\n"
            "\n",
            encoding="utf-8",
        )
    return p


def voice_profile_path() -> Path:
    return data_dir() / "my_voice.npy"

