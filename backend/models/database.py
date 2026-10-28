from sqlalchemy import create_engine, inspect, text, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import json
import os

# Using file-based DB for persistence
# We will derive the DB path dynamically from the environment or a default
import sys

def get_bootstrap_dir():
    """Thư mục bootstrap CỐ ĐỊNH — chỉ chứa 2 file nhỏ mà Electron phải tìm được
    TRƯỚC khi biết data root: storage.json + port.json. Mọi dữ liệu khác (db, config,
    presets, voices, models, dự án…) nằm chung trong data root (xem get_app_data_dir).

    TÁCH dev ↔ bản cài: DEV -> %APPDATA%/SubForge-Dev, đóng gói -> %APPDATA%/SubForge
    (khớp Electron main.js APP_DATA_FOLDER + utils/storage_paths._app_data_dir)."""
    dev = os.environ.get("SUBFORGE_DEV") == "true"
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return os.path.join(appdata, "SubForge-Dev" if dev else "SubForge")
    # Fallback to home directory on Unix/Mac
    home = os.path.expanduser('~')
    return os.path.join(home, '.subforge-dev' if dev else '.subforge')


# Các file/thư mục NHỎ cần mang theo khi hợp nhất APPDATA cũ -> <data_root>/app.
# KHÔNG copy: storage.json/port.json (bootstrap), ai_models (cache tự sinh lại),
# *.log (không quan trọng), dubbing/review (dữ liệu nặng đã nằm theo config riêng).
_SEED_ITEMS = (
    "subforge.db", "config.json", "douyin_cookies.txt",
    "compositor_personal_presets.json", "compositor_presets",
    "voices", "usage_reports.json", "usage_ledger.json",
)

_app_dir_cache = None


def get_app_data_dir():
    """Thư mục dữ liệu ứng dụng (db, config, presets, voices, cookies…).

    HỢP NHẤT MỘT THƯ MỤC: khi người dùng đã chọn "Vị trí lưu dữ liệu" (storage.json
    có data_root) thì mọi thứ nằm ở <data_root>/app — cùng chỗ với ai_models /
    gpu_pack / dubbing / review. Lần đầu chuyển sang cơ chế này, các file nhỏ từ
    %APPDATA%/SubForge cũ được COPY sang (db/config/presets/voices) để người dùng
    cũ không mất dự án; bản gốc giữ nguyên (an toàn, dung lượng nhỏ).

    Chưa cấu hình data_root (lần đầu cài, chưa chọn vị trí) -> dùng APPDATA cũ."""
    global _app_dir_cache
    if _app_dir_cache:
        return _app_dir_cache

    legacy = get_bootstrap_dir()
    data_root = ""
    try:
        with open(os.path.join(legacy, "storage.json"), "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if isinstance(cfg, dict):
            data_root = str(cfg.get("data_root") or "").strip()
    except Exception:
        data_root = ""

    if not data_root:
        _app_dir_cache = legacy
        return legacy

    app_dir = os.path.join(os.path.abspath(data_root), "app")
    try:
        os.makedirs(app_dir, exist_ok=True)
        # Seed 1 lần: db chưa có ở chỗ mới mà APPDATA cũ có -> copy các file nhỏ.
        if not os.path.exists(os.path.join(app_dir, "subforge.db")) and os.path.isdir(legacy):
            import shutil
            for name in _SEED_ITEMS:
                src = os.path.join(legacy, name)
                dst = os.path.join(app_dir, name)
                if not os.path.exists(src) or os.path.exists(dst):
                    continue
                try:
                    if os.path.isdir(src):
                        shutil.copytree(src, dst)
                    else:
                        shutil.copy2(src, dst)
                except OSError:
                    pass
    except OSError:
        # Ổ đĩa data root lỗi/rút mất -> lui về APPDATA để app vẫn chạy được.
        _app_dir_cache = legacy
        return legacy

    _app_dir_cache = app_dir
    return app_dir

def get_db_path():
    app_dir = get_app_data_dir()
    os.makedirs(app_dir, exist_ok=True)
    db_file = os.path.join(app_dir, "subforge.db")
    return f"sqlite:///{db_file}"

engine = create_engine(
    get_db_path(),
    connect_args={"check_same_thread": False}
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _connection_record):
    """Bật WAL + busy_timeout cho MỌI kết nối SQLite.

    - journal_mode=WAL: cho phép ĐỌC đồng thời trong khi GHI (poll GET /videos/{id}
      mỗi 4s đọc trong lúc STT/TTS commit tăng dần) → hết 'database is locked'.
    - busy_timeout=5000: nếu vẫn kẹt khoá, chờ tối đa 5s thay vì lỗi ngay.
    """
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
    except Exception as e:
        print(f"[DB] Không set được PRAGMA (bỏ qua): {e}")


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Define Base here, don't import it from models
Base = declarative_base()

def _sqlite_type(col_type) -> str:
    """Map kiểu cột SQLAlchemy sang kiểu SQLite cho ALTER TABLE ADD COLUMN."""
    import sqlalchemy as sa
    if isinstance(col_type, (sa.Integer, sa.Boolean)):
        return "INTEGER"
    if isinstance(col_type, sa.Float):
        return "FLOAT"
    return "TEXT"


def _run_lightweight_migrations():
    """Thêm các cột còn THIẾU vào những bảng ĐÃ tồn tại (kịch bản nâng cấp).

    `Base.metadata.create_all` chỉ tạo bảng mới chứ KHÔNG thêm cột vào bảng cũ.
    Nếu bản mới thêm cột (vd: duration, speed_mode, bg_volume...) mà DB của người
    dùng cũ chưa có, mọi truy vấn sẽ lỗi 'no such column' -> app 500. Hàm này so
    sánh cột trong model với cột thực tế và ALTER TABLE ADD COLUMN cho phần thiếu.
    Idempotent, an toàn chạy mỗi lần khởi động.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table_name, table in Base.metadata.tables.items():
        if table_name not in existing_tables:
            continue  # create_all đã/sẽ tự tạo bảng mới đầy đủ cột
        existing_cols = {c["name"] for c in inspector.get_columns(table_name)}
        for col in table.columns:
            if col.name in existing_cols:
                continue
            ddl = f'ALTER TABLE "{table_name}" ADD COLUMN "{col.name}" {_sqlite_type(col.type)}'
            default = None
            if col.default is not None and hasattr(col.default, "arg") and not callable(col.default.arg):
                default = col.default.arg
            if default is not None:
                if isinstance(default, bool):
                    ddl += f" DEFAULT {1 if default else 0}"
                elif isinstance(default, (int, float)):
                    ddl += f" DEFAULT {default}"
                else:
                    safe = str(default).replace("'", "''")
                    ddl += f" DEFAULT '{safe}'"
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
                # ASCII-only: tránh UnicodeEncodeError khi stdout dùng codec cp932
                # (Windows locale Nhật) -> nếu print tiếng Việt sẽ crash startup.
                print(f"[MIGRATION] Added missing column: {table_name}.{col.name}")
            except Exception as e:
                print(f"[MIGRATION] Skipped {table_name}.{col.name}: {e}")


def _reset_stuck_processing():
    """Đưa các phase đang kẹt 'processing' (do tắt đột ngột lần trước) về 'error'.

    Nếu không reset, UI sẽ hiển thị spinner mãi mãi và stream log không bao giờ
    đóng. Sau khi reset, người dùng có thể bấm chạy lại bước đó.

    Ngoài ra ghi 1 dòng cuối vào synthesis.log của từng video bị treo — vì log cũ
    dừng giữa chừng nên mở lại trông như đang chạy; dòng này báo rõ đã gián đoạn.
    """
    try:
        cols = ["p1_status", "p2_status", "p3_status", "p4_status", "p5_status"]
        where = " OR ".join([f"{c} = 'processing'" for c in cols])
        # 1) Lấy id các video đang treo TRƯỚC khi reset để ghi log gián đoạn.
        stuck_ids = []
        with engine.begin() as conn:
            rows = conn.execute(text(f"SELECT id FROM videos WHERE {where}")).fetchall()
            stuck_ids = [r[0] for r in rows]
        # 2) Ghi dòng "bị gián đoạn" vào synthesis.log từng video (best-effort).
        if stuck_ids:
            _append_dubbing_interrupt_logs(stuck_ids)
        # 3) Reset trạng thái treo -> 'error' để UI thoát spinner, cho chạy lại.
        sets = ", ".join([f"{c} = 'error'" for c in cols])
        with engine.begin() as conn:
            conn.execute(text(f"UPDATE videos SET {sets} WHERE {where}"))
    except Exception as e:
        print(f"[STARTUP] Could not reset stuck 'processing' status: {e}")


def _append_dubbing_interrupt_logs(video_ids):
    """Ghi 1 dòng [ERROR] báo gián đoạn vào synthesis.log của từng video lồng tiếng
    bị treo (log dừng giữa chừng nên trông như đang chạy). Best-effort, không chặn."""
    try:
        from datetime import datetime
        from services.config_service import get_config_service
        root = get_config_service().get_dubbing_root()
        if not root:
            return
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for vid in video_ids:
            try:
                log_file = os.path.join(root, str(vid), "synthesis.log")
                if not os.path.exists(log_file):
                    continue
                with open(log_file, "a", encoding="utf-8") as f:
                    f.write(
                        f"{ts} [ERROR] DubbingEngine: === XỬ LÝ LỒNG TIẾNG bị gián đoạn "
                        f"do ứng dụng đóng giữa chừng. Hãy chạy lại bước đang dở. ===\n"
                    )
            except OSError:
                continue
    except Exception as e:
        print(f"[STARTUP] Could not append dubbing interrupt logs: {e}")


def init_db():
    # Import models here locally to avoid circular import
    from . import models
    Base.metadata.create_all(bind=engine)
    # Migration/reset là best-effort: lỗi ở đây KHÔNG được làm chết startup.
    try:
        _run_lightweight_migrations()
    except Exception as e:
        print(f"[MIGRATION] Lightweight migration failed (non-fatal): {e}")
    try:
        _reset_stuck_processing()
    except Exception as e:
        print(f"[STARTUP] Stuck-status reset failed (non-fatal): {e}")

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
