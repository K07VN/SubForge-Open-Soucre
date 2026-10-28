"""Runtime setup for the editable source reconstruction.

This module deliberately contains no bytecode loader and never reads a
``.marshal``/``PYZ.pyz`` file. The shipped first-party modules are ordinary
``.py`` files in ``runtime/backend/_internal`` and the editable tree.
"""
from pathlib import Path
from contextlib import closing
import json
import os
import re
import sqlite3
import sys

RECOVERED = Path(__file__).resolve().parents[1]
INSTALL = RECOVERED.parent
RUNTIME = RECOVERED / "runtime/backend"
INTERNAL = RUNTIME / "_internal"
CUDA_RUNTIME = RECOVERED / "local-data/runtime/cuda-pascal"
_dll_handles = []


def _rebase_local_data(old_root: str, new_root: Path) -> None:
    """Rebase only paths inside the old bundle, preserving external media paths."""
    old_abs = os.path.abspath(old_root).rstrip("\\/")
    new_abs = os.path.abspath(str(new_root)).rstrip("\\/")
    if os.path.normcase(old_abs) == os.path.normcase(new_abs):
        return
    mappings = [(old_abs, new_abs)]
    if Path(old_abs).parts[-2:] == ("local-data", "data"):
        mappings.append((str(Path(old_abs).parents[1]), str(new_root.parents[1])))
    replacements = []
    for old, new in mappings:
        for a, b in ((old, new), (old.replace("\\", "/"), new.replace("\\", "/")),
                     (old.replace("\\", "\\\\"), new.replace("\\", "\\\\"))):
            replacements.append((re.compile(re.escape(a) + r"(?=$|[\\/\"'])", re.I), b))

    def rebase_text(value):
        for pattern, replacement in replacements:
            value = pattern.sub(lambda _match: replacement, value)
        return value

    def rebase_json(value):
        if isinstance(value, str):
            return rebase_text(value)
        if isinstance(value, list):
            return [rebase_json(item) for item in value]
        if isinstance(value, dict):
            return {key: rebase_json(item) for key, item in value.items()}
        return value

    for folder, dirs, files in os.walk(new_root):
        dirs[:] = [name for name in dirs if name not in {"ai_models", "relocation-backups"}]
        for name in files:
            path = Path(folder) / name
            if path.suffix.lower() != ".json":
                continue
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            updated = rebase_json(value)
            if value != updated:
                temp = path.with_name(path.name + ".relocating")
                temp.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
                temp.replace(path)
    db = new_root / "app/subforge.db"
    if not db.is_file():
        return
    with closing(sqlite3.connect(str(db), timeout=10)) as connection:
        connection.create_function("portable_rebase", 1, lambda text: rebase_text(text) if isinstance(text, str) else text)
        for (table,) in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            if not table or table.startswith("sqlite_"):
                continue
            for column in connection.execute(f'PRAGMA table_info("{table}")').fetchall():
                name, kind = column[1], str(column[2]).upper()
                if not any(token in kind for token in ("CHAR", "TEXT", "CLOB")):
                    continue
                qt, qc = table.replace('"', '""'), name.replace('"', '""')
                connection.execute(f'UPDATE "{qt}" SET "{qc}"=portable_rebase("{qc}") WHERE typeof("{qc}")=\'text\'')
        connection.commit()


def selected_cuda_runtime():
    """Use a tested local CUDA overlay; an explicit path permits staging probes."""
    explicit = os.environ.get("SUBFORGE_CUDA_RUNTIME")
    if (os.environ.get("SUBFORGE_DISABLE_CUDA_RUNTIME") == "1"
            or os.environ.get("CUDA_VISIBLE_DEVICES") in ("", "-1")):
        return None
    if not explicit and (RECOVERED / "local-data/data/uninstall_gpu.flag").exists():
        return None
    candidate = Path(explicit).resolve() if explicit else CUDA_RUNTIME
    if explicit or (candidate / "verified.json").is_file():
        if not (candidate / "torch/__init__.py").is_file():
            raise RuntimeError(f"CUDA runtime is incomplete: {candidate}")
        return candidate
    return None


def setup_environment():
    state = RECOVERED / "local-data"
    bootstrap = state / "bootstrap"
    data_root = state / "data"
    appdata = bootstrap / "SubForge-Dev"
    appdata.mkdir(parents=True, exist_ok=True)
    data_root.mkdir(parents=True, exist_ok=True)
    storage = appdata / "storage.json"
    previous_root = ""
    if storage.is_file():
        try:
            previous_root = str(json.loads(storage.read_text(encoding="utf-8")).get("data_root") or "")
        except Exception:
            pass
    if previous_root and os.path.normcase(os.path.abspath(previous_root)) != os.path.normcase(os.path.abspath(str(data_root))):
        _rebase_local_data(previous_root, data_root)
    if previous_root != str(data_root):
        storage.write_text(json.dumps({"data_root": str(data_root)}, ensure_ascii=False), encoding="utf-8")
    os.environ.update({
        "APPDATA": str(bootstrap), "SUBFORGE_DEV": "true",
        "SUBFORGE_DATA_ROOT": str(data_root),
        "SUBFORGE_MODELS_DIR": str(data_root / "ai_models"),
        "SUBFORGE_APP_VERSION": "2.15.14-local",
        "HF_HOME": str(data_root / "ai_models/hf"),
        "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1", "DO_NOT_TRACK": "1",
        "HF_HUB_DISABLE_XET": "1", "SUBFORGE_ADMIN_URL": "http://127.0.0.1:9",
        "SUBFORGE_DESKTOP": "true",
    })
    os.environ.pop("SUBFORGE_FORCE_LICENSE_VERIFY", None)
    # Honor the UI CPU switch before install_local_services imports torch and
    # initializes CUDA; setting this only later in main.py would be too late.
    if any((data_root / flag).exists() for flag in ("force_cpu.flag", "uninstall_gpu.flag")):
        os.environ["CUDA_VISIBLE_DEVICES"] = ""
    return data_root


def setup_dependencies():
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Run with py -3.12; the shipped native modules use cp312.")
    if not INTERNAL.is_dir():
        raise RuntimeError(f"Copied source/runtime is missing: {INTERNAL}")
    backend = RECOVERED / "backend"
    cuda = selected_cuda_runtime()
    paths = (INTERNAL, cuda, backend) if cuda else (INTERNAL, backend)
    for path in paths:
        while str(path) in sys.path:
            sys.path.remove(str(path))
        sys.path.insert(0, str(path))
    if os.name == "nt":
        torch_lib = (cuda if cuda else INTERNAL) / "torch/lib"
        for directory in (INTERNAL, torch_lib, INTERNAL / "ctranslate2",
                          INTERNAL / "onnxruntime/capi", INTERNAL / "numpy.libs",
                          INTERNAL / "scipy.libs", INTERNAL / "av.libs"):
            if directory.is_dir():
                _dll_handles.append(os.add_dll_directory(str(directory)))
        if cuda:
            # CTranslate2 loads cuBLAS/cuDNN by DLL name; use the matching local
            # torch distribution's DLLs, without installing a system CUDA toolkit.
            os.environ["PATH"] = str(torch_lib) + os.pathsep + os.environ.get("PATH", "")


def bootstrap():
    sys.dont_write_bytecode = True
    setup_environment()
    setup_dependencies()


if __name__ == "__main__":
    bootstrap()
    import fastapi, numpy, torch, ctranslate2, onnxruntime
    print(json.dumps({m.__name__: getattr(m, "__version__", "loaded")
                      for m in (fastapi, numpy, torch, ctranslate2, onnxruntime)}))
