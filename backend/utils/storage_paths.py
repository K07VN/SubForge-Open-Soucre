"""Vị trí lưu trữ dữ liệu nặng (ai_models / gpu_pack / dubbing / review).

Người dùng chọn MỘT thư mục gốc (data root) — mặc định C:/SubForge_Workstation_Data.
Lựa chọn được lưu ở %APPDATA%/SubForge/storage.json để CẢ Electron (đọc TRƯỚC khi
spawn backend — quyết định exe GPU nào chạy) lẫn backend cùng thấy.

Thứ tự ưu tiên get_data_root():
  1. Env SUBFORGE_DATA_ROOT (Electron truyền lúc spawn).
  2. storage.json (chạy chế độ browser / dev không qua Electron).
  3. Mặc định cũ: C:/SubForge_Workstation_Data[_Dev] — tương thích ngược 100%.

gpu_pack được COPY nóng ngay lúc migrate (Windows cho ĐỌC DLL đang nạp, chỉ chặn
xóa/rename) — phần chưa dọn được (nguồn cũ bị khóa) ghi vào "pending_cleanup" trong
storage.json, lần khởi động SAU (trước khi nạp DLL) xóa nốt. Marker
"pending_gpu_pack_move" giữ lại làm đường lui: nếu vì lý do nào đó copy nóng không
thực hiện được, boot sau sẽ move trọn gói.
"""
from utils.i18n import L
import json
import logging
import os
import shutil
import sys

logger = logging.getLogger("StoragePaths")

_LEGACY_DEFAULT = "C:/SubForge_Workstation_Data"
_LEGACY_DEFAULT_DEV = "C:/SubForge_Workstation_Data_Dev"


def _app_data_dir() -> str:
    # Copy nhỏ của models.database.get_app_data_dir — tránh import DB (nặng, tạo engine)
    # chỉ để lấy 1 đường dẫn, và tránh vòng import khi main.py cần hàm này RẤT sớm.
    # TÁCH dev ↔ bản cài: DEV -> SubForge-Dev, đóng gói -> SubForge (khớp Electron
    # main.js APP_DATA_FOLDER). Trước đây dùng CHUNG nên cache/log/con trỏ giẫm lên nhau.
    dev = os.environ.get("SUBFORGE_DEV") == "true"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "SubForge-Dev" if dev else "SubForge")
    return os.path.join(os.path.expanduser("~"), ".subforge-dev" if dev else ".subforge")


def storage_config_path() -> str:
    # Thư mục đã tách theo chế độ (SubForge vs SubForge-Dev) nên chỉ cần storage.json.
    return os.path.join(_app_data_dir(), "storage.json")


def _load_storage_config() -> dict:
    try:
        with open(storage_config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_storage_config(cfg: dict) -> None:
    os.makedirs(_app_data_dir(), exist_ok=True)
    with open(storage_config_path(), "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_default_data_root() -> str:
    return (
        _LEGACY_DEFAULT_DEV
        if os.environ.get("SUBFORGE_DEV") == "true"
        else _LEGACY_DEFAULT
    )


def _drive_exists(path: str) -> bool:
    try:
        drive, _ = os.path.splitdrive(path)
        return (not drive) or os.path.exists(drive + os.sep)
    except Exception:
        return False


def _is_dev() -> bool:
    return os.environ.get("SUBFORGE_DEV") == "true"


def _stored_data_root() -> str:
    """data_root từ storage.json, NHƯNG bỏ qua nếu là thư mục '_Dev' mà đang chạy
    bản KHÔNG-dev (di sản file storage.json cũ dùng chung dev↔.exe). Tránh bản cài
    lưu nhầm vào thư mục dev."""
    root = str(_load_storage_config().get("data_root") or "").strip()
    if root and not _is_dev() and root.rstrip("\\/").lower().endswith("_dev"):
        return ""
    return root


def get_data_root() -> str:
    """Thư mục gốc dữ liệu nặng. Ổ đĩa đã cấu hình không còn (rút USB/ổ rời) →
    tự lui về mặc định C: để app vẫn khởi động được (giống ConfigService)."""
    root = (os.environ.get("SUBFORGE_DATA_ROOT") or "").strip()
    if root and not _is_dev() and root.rstrip("\\/").lower().endswith("_dev"):
        root = ""  # bản cài không dùng thư mục dev (di sản)
    if not root:
        root = _stored_data_root()
    if not root:
        root = get_default_data_root()
    root = os.path.abspath(root)
    if not _drive_exists(root):
        logger.warning(
            "Ổ đĩa của data root '%s' không tồn tại — dùng mặc định %s",
            root, get_default_data_root(),
        )
        return os.path.abspath(get_default_data_root())
    return root


def get_workstation_dir() -> str:
    """Alias ngữ nghĩa cũ (WORKSTATION_DIR) — nơi chứa gpu_pack + các flag."""
    return get_data_root()


def is_configured() -> bool:
    """Người dùng ĐÃ chọn vị trí lưu dữ liệu chưa (storage.json có data_root)?

    Dựa vào FILE storage.json chứ không dựa env SUBFORGE_DATA_ROOT — Electron
    luôn truyền env (kể cả giá trị mặc định khi chưa chọn) nên env không phân
    biệt được "đã chọn" với "mặc định tạm". Root '_Dev' bị bỏ qua ở bản đóng gói
    (di sản) -> coi như CHƯA cấu hình để hiện lại màn chọn vị trí lưu."""
    return bool(_stored_data_root())


def _has_real_user_data(root: str) -> bool:
    """Thư mục có DỮ LIỆU THẬT của người dùng cũ không (để tự-nhận, không hỏi lại)?

    CHỈ tính dữ liệu thật (db đã dùng, model đã tải, dự án đã có) — KHÔNG tính khung
    thư mục rỗng (app/dubbing/review) mà app tự tạo lúc khởi động. Nếu tính cả khung
    rỗng thì máy vừa xóa sạch vẫn bị coi là "đã có dữ liệu" -> không bao giờ hỏi lại
    vị trí lưu (bug)."""
    if not os.path.isdir(root):
        return False
    # DB đã tồn tại = đã dùng app trước đó.
    if os.path.isfile(os.path.join(root, "app", "subforge.db")):
        return True
    # Model AI đã tải, hoặc có dự án Lồng tiếng/Review thật (thư mục con bên trong).
    for sub in ("ai_models", "gpu_pack", "dubbing", "review"):
        d = os.path.join(root, sub)
        try:
            if os.path.isdir(d) and any(os.scandir(d)):
                return True
        except OSError:
            continue
    return False


def adopt_legacy_root_if_needed() -> None:
    """Người dùng CŨ (đã có DỮ LIỆU THẬT ở thư mục mặc định nhưng chưa từng ghi
    storage.json) -> tự nhận thư mục mặc định làm data root, không hỏi lại.
    Máy MỚI (chưa có dữ liệu thật, chỉ khung rỗng) -> KHÔNG tự nhận -> hiện màn chọn.
    Gọi RẤT SỚM lúc khởi động — trước khi mở DB (get_app_data_dir đọc file này)."""
    if is_configured():
        return
    default_root = os.path.abspath(get_default_data_root())
    if _has_real_user_data(default_root):
        set_data_root(default_root)
        logger.info("Đã tự nhận thư mục dữ liệu cũ làm data root: %s", default_root)


def is_default_data_root() -> bool:
    return os.path.normcase(get_data_root()) == os.path.normcase(
        os.path.abspath(get_default_data_root())
    )


def set_data_root(path: str, pending_gpu_pack_from: str | None = None) -> None:
    """Ghi data root mới vào storage.json (gọi SAU khi migrate xong các phần không khóa).

    pending_gpu_pack_from: thư mục gốc CŨ còn chứa gpu_pack — ghi marker để lần
    khởi động sau move nốt (gpu_pack đang bị khóa trong tiến trình hiện tại).
    """
    cfg = _load_storage_config()
    cfg["data_root"] = os.path.abspath(path)
    if pending_gpu_pack_from:
        cfg["pending_gpu_pack_move"] = {"from": os.path.abspath(pending_gpu_pack_from)}
    else:
        cfg.pop("pending_gpu_pack_move", None)
    _save_storage_config(cfg)
    # Cùng tiến trình: cho get_data_root() thấy ngay giá trị mới (env cũ sẽ ghi đè
    # sai nếu còn) — các request sau restart thì Electron truyền env mới.
    os.environ["SUBFORGE_DATA_ROOT"] = cfg["data_root"]


def add_pending_cleanup(path: str) -> None:
    """Nguồn không xóa được (file đang mmap bởi model đã nạp) — ghi lại để lần
    khởi động sau dọn nốt."""
    cfg = _load_storage_config()
    items = cfg.get("pending_cleanup") or []
    ap = os.path.abspath(path)
    if ap not in items:
        items.append(ap)
    cfg["pending_cleanup"] = items
    _save_storage_config(cfg)


def check_render_disk_space(
    work_dir: str,
    source_paths: tuple = (),
    factor: float = 2.0,
    min_free_mb: float = 1024.0,
) -> None:
    """Kiểm dung lượng trống TRƯỚC khi render/xuất video — video lớn mà hết đĩa
    giữa chừng sẽ ra file hỏng + lỗi khó hiểu.

    Cần trống >= max(min_free_mb, tổng size các file nguồn × factor) — render giữ
    đồng thời file tạm (wav/clip) + file xuất nên ~2x nguồn là ước lượng an toàn.
    Thiếu -> raise RuntimeError thông điệp tiếng Việt (handler sẵn có sẽ hiển thị).
    """
    try:
        os.makedirs(work_dir, exist_ok=True)
        free = shutil.disk_usage(work_dir).free
    except OSError:
        return  # không đo được (ổ lạ) — không chặn render
    src_bytes = 0
    for p in source_paths:
        try:
            if p and os.path.isfile(p):
                src_bytes += os.path.getsize(p)
        except OSError:
            pass
    need = max(int(min_free_mb * 1024 * 1024), int(src_bytes * factor))
    if free < need:
        raise RuntimeError(
            L(f"Ổ đĩa không đủ dung lượng trống để dựng video: cần ~{need / 1024 ** 3:.1f}GB, "
            f"còn {free / 1024 ** 3:.1f}GB tại '{work_dir}'. Hãy giải phóng bớt dung lượng, "
            "hoặc đổi \"Vị trí lưu dữ liệu\" sang ổ khác (Cài đặt → Cấu hình lưu trữ) rồi thử lại.", f"Not enough free disk space to build the video: need ~{need / 1024 ** 3:.1f}GB, {free / 1024 ** 3:.1f}GB left at '{work_dir}'. Free up some space, or change the \"Data storage location\" to another drive (Settings → Storage configuration) and try again.")
        )


def remove_pending_cleanup(path: str) -> None:
    """Gỡ path khỏi danh sách dọn-khi-khởi-động. GỌI khi bắt đầu TẢI LẠI model vào
    đúng thư mục đó — nếu không, lần boot sau sẽ xóa nhầm bản vừa tải mới."""
    cfg = _load_storage_config()
    items = cfg.get("pending_cleanup") or []
    ap = os.path.normcase(os.path.abspath(path))
    remain = [p for p in items if os.path.normcase(os.path.abspath(p)) != ap]
    if len(remain) != len(items):
        if remain:
            cfg["pending_cleanup"] = remain
        else:
            cfg.pop("pending_cleanup", None)
        _save_storage_config(cfg)


def consume_boot_migrations() -> None:
    """Gọi RẤT SỚM lúc backend khởi động — TRƯỚC khi nạp DLL CUDA / import torch.

    1. pending_gpu_pack_move: move gpu_pack (+ flags) từ root cũ sang root mới.
    2. pending_cleanup: xóa các thư mục nguồn còn sót của lần migrate trước.
    Lỗi ở đây không được chặn app khởi động — log rồi bỏ marker.
    """
    cfg = _load_storage_config()
    changed = False

    pending = cfg.get("pending_gpu_pack_move")
    if isinstance(pending, dict) and pending.get("from"):
        old_root = str(pending["from"])
        new_root = get_data_root()
        try:
            if os.path.normcase(os.path.abspath(old_root)) != os.path.normcase(new_root):
                for name in ("gpu_pack", "uninstall_gpu.flag", "force_cpu.flag"):
                    src = os.path.join(old_root, name)
                    dst = os.path.join(new_root, name)
                    if not os.path.exists(src) or os.path.exists(dst):
                        continue
                    os.makedirs(new_root, exist_ok=True)
                    try:
                        os.rename(src, dst)  # cùng ổ: tức thì
                    except OSError:
                        if os.path.isdir(src):
                            shutil.copytree(src, dst)
                            shutil.rmtree(src, ignore_errors=True)
                        else:
                            shutil.copy2(src, dst)
                            try:
                                os.remove(src)
                            except OSError:
                                pass
                logger.info("Đã chuyển gpu_pack từ %s sang %s", old_root, new_root)
        except Exception as e:
            # Không chuyển được → bỏ marker; gói GPU có thể tải lại từ Cài đặt.
            logger.error("Không chuyển được gpu_pack (%s) — có thể cần tải lại gói GPU.", e)
        cfg.pop("pending_gpu_pack_move", None)
        changed = True

    leftovers = cfg.get("pending_cleanup") or []
    if leftovers:
        remain = []
        for p in leftovers:
            try:
                if os.path.isdir(p):
                    shutil.rmtree(p)
                elif os.path.isfile(p):
                    os.remove(p)
            except Exception as e:
                logger.warning("Chưa dọn được %s (%s) — thử lại lần sau.", p, e)
                remain.append(p)
        if remain:
            cfg["pending_cleanup"] = remain
        else:
            cfg.pop("pending_cleanup", None)
        changed = True

    if changed:
        try:
            _save_storage_config(cfg)
        except Exception as e:
            logger.error("Không ghi được storage.json: %s", e)
