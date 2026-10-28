# Decompiled from the shipped Python 3.12 entrypoint; see main-recovery verification.
# Formatting/comments are reconstructed; behavior is verified by recompilation.
import os
import sys
from dotenv import load_dotenv

if not getattr(sys, 'frozen', False):
    load_dotenv(os.path.join(os.path.dirname(__file__), '../.env'))
import socket
import time
import webbrowser
import signal
import threading
import json
import modulefinder

try:
    from utils.model_paths import apply_vieneu_hf_env
    apply_vieneu_hf_env()
except Exception:
    pass

for _stream in (sys.stdout, sys.stderr):
    try:
        if _stream and hasattr(_stream, 'reconfigure'):
            _stream.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

if sys.platform == 'win32':
    for stream in (sys.stdout, sys.stderr):
        if not stream:
            continue
        if hasattr(stream, 'flush'):
            orig_flush = stream.flush

            def patched_flush(*a, _f=orig_flush, **k):
                try:
                    _f(*a, **k)
                except OSError as e:
                    if e.errno != 22:
                        raise

            stream.flush = patched_flush
        if hasattr(stream, 'write'):
            orig_write = stream.write

            def patched_write(s, _w=orig_write):
                try:
                    return _w(s)
                except OSError as e:
                    if e.errno == 22:
                        return len(s)
                    raise

            stream.write = patched_write

import logging
import traceback
import imageio_ffmpeg
import uvicorn
import shutil
import subprocess as _subprocess

if sys.platform == 'win32':
    _CREATE_NO_WINDOW = getattr(_subprocess, 'CREATE_NO_WINDOW', 134217728)
    _orig_popen_init = _subprocess.Popen.__init__

    def _popen_no_window(self, *args, **kwargs):
        if not kwargs.get('creationflags'):
            kwargs['creationflags'] = _CREATE_NO_WINDOW
        else:
            kwargs['creationflags'] |= _CREATE_NO_WINDOW
        _orig_popen_init(self, *args, **kwargs)

    _subprocess.Popen.__init__ = _popen_no_window

SUBFORGE_DEV = os.environ.get('SUBFORGE_DEV') == 'true'
from utils.ssl_tin_cay import cai_cho_urllib
cai_cho_urllib()
from utils.storage_paths import adopt_legacy_root_if_needed, consume_boot_migrations, get_workstation_dir
adopt_legacy_root_if_needed()
consume_boot_migrations()
WORKSTATION_DIR = get_workstation_dir()
if (os.path.exists(os.path.join(WORKSTATION_DIR, 'force_cpu.flag')) or
        os.path.exists(os.path.join(WORKSTATION_DIR, 'uninstall_gpu.flag'))):
    os.environ['CUDA_VISIBLE_DEVICES'] = ''


def _register_cuda_dll_dirs() -> None:
    if sys.platform != 'win32':
        return
    gpu_pack_dir = os.path.join(WORKSTATION_DIR, 'gpu_pack', 'subforge_backend_gpu')
    internal_dir = os.path.join(gpu_pack_dir, '_internal')
    candidates = []
    if os.path.isdir(internal_dir):
        import shutil
        for folder in ('routes', 'services', 'models', 'utils', 'db', 'rapidocr', 'torchao'):
            target = os.path.join(internal_dir, folder)
            if os.path.isdir(target):
                try:
                    shutil.rmtree(target, ignore_errors=True)
                except Exception:
                    pass
        for file in ('main.pyc', 'main.py', 'main.exe', 'subforge_backend_gpu.exe'):
            target_file = os.path.join(internal_dir, file)
            if os.path.isfile(target_file):
                try:
                    os.remove(target_file)
                except Exception:
                    pass
        print(f'[GPU] Phat hien goi thu vien CUDA ngoai: {internal_dir}')
        if internal_dir not in sys.path:
            sys.path.insert(0, internal_dir)
        candidates.append(internal_dir)
        nvidia_root = os.path.join(internal_dir, 'nvidia')
        if os.path.isdir(nvidia_root):
            for pkg in os.listdir(nvidia_root):
                for sub in ('bin', 'lib'):
                    d = os.path.join(nvidia_root, pkg, sub)
                    if os.path.isdir(d):
                        candidates.append(d)
        torch_lib = os.path.join(internal_dir, 'torch', 'lib')
        if os.path.isdir(torch_lib):
            candidates.append(torch_lib)
    base = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    nvidia_root = os.path.join(base, 'nvidia')
    if os.path.isdir(nvidia_root):
        for pkg in os.listdir(nvidia_root):
            for sub in ('bin', 'lib'):
                d = os.path.join(nvidia_root, pkg, sub)
                if os.path.isdir(d):
                    candidates.append(d)
    torch_lib = os.path.join(base, 'torch', 'lib')
    if os.path.isdir(torch_lib):
        candidates.append(torch_lib)
    for d in candidates:
        try:
            if hasattr(os, 'add_dll_directory'):
                os.add_dll_directory(d)
            if d not in os.environ['PATH']:
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')
        except Exception:
            pass


_register_cuda_dll_dirs()
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

try:
    uninstall_flag = os.path.join(WORKSTATION_DIR, 'uninstall_gpu.flag')
    gpu_pack_dir = os.path.join(WORKSTATION_DIR, 'gpu_pack')
    if os.path.exists(uninstall_flag):
        if os.path.exists(gpu_pack_dir):
            shutil.rmtree(gpu_pack_dir, ignore_errors=True)
        os.remove(uninstall_flag)
except Exception as e:
    pass

logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S')
logger = logging.getLogger('SubForge')


class _ErrorLogDBHandler(logging.Handler):
    '''Bắt mọi log mức ERROR và đẩy sang usage_reporter để gửi lên server (bảng
    api_key_usage_logs.error_log) — cùng cơ chế/chu kỳ với thống kê voice/review/
    lồng-tiếng. Dedupe + cắt trần + chỉ gửi khi đã kích hoạt (nằm trong usage_reporter).
    '''

    def emit(self, record):
        try:
            if record.name == 'UsageReporter':
                return
            msg = record.getMessage()
            if record.name == 'asyncio':
                if '_call_connection_lost' in msg or 'connection_lost' in msg:
                    return
            from services import usage_reporter
            usage_reporter.record_error(f'{record.name}: {record.getMessage()}')
        except Exception:
            pass


_error_log_db_handler = _ErrorLogDBHandler(level=logging.ERROR)
logging.getLogger().addHandler(_error_log_db_handler)


def log_crash(e: Exception):
    '''Ghi lại vết lỗi khi hệ thống gặp sự cố nghiêm trọng'''
    with open('backend_crash.log', 'a', encoding='utf-8') as f:
        f.write('\n==================================================\n')
        f.write(f'CRASH AT: {os.path.abspath(os.getcwd())}\n')
        f.write(traceback.format_exc())
        f.write('==================================================\n')


try:
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    if ffmpeg_dir not in os.environ['PATH']:
        os.environ['PATH'] = ffmpeg_dir + os.pathsep + os.environ['PATH']
    logger.info(f'[SYSTEM] FFmpeg path chuẩn bị sẵn sàng: {ffmpeg_dir}')
except Exception as e:
    logger.error(f'[SYSTEM] Lỗi khởi tạo FFmpeg: {e}')
    ffmpeg_exe = 'Not Found'


def _release_all_gpu_models() -> None:
    '''Giai phong VRAM khi backend tat (Electron quit / uvicorn reload).'''
    try:
        from services.stt_service import get_stt_service
        get_stt_service().release_gpu(sync_timeout_sec=3.0)
    except Exception as e:
        logger.warning(f'[GPU] release Whisper that bai (bo qua): {e}')
    try:
        from services.vieneu_service import VieneuService
        with VieneuService._lock:
            if VieneuService._tts is not None:
                VieneuService._release_engine(VieneuService._tts)
                VieneuService._tts = None
                VieneuService._registered_clones.clear()
    except Exception as e:
        logger.warning(f'[GPU] release VieNeu that bai (bo qua): {e}')
    try:
        from services.separation_service import SeparationService
        SeparationService.release_gpu()
    except Exception as e:
        logger.warning(f'[GPU] release Demucs that bai (bo qua): {e}')
    try:
        from services.qwen_tts_service import QwenTTSService
        QwenTTSService.stop()
    except Exception as e:
        logger.warning(f'[GPU] tat Qwen3-TTS that bai (bo qua): {e}')


@asynccontextmanager
async def _app_lifespan(_app: FastAPI):
    try:
        from services.usage_reporter import start_worker
        start_worker()
    except Exception as _e:
        logger.warning(f'Không khởi động được UsageReporter worker: {_e}')
    try:
        from routes.review_routes import review_service as _rs
        _rs.recover_stale_states()
    except Exception as _e:
        logger.warning(f'Recovery trạng thái review lỗi (bỏ qua): {_e}')
    try:
        from routes.review_routes import review_service as _rs2
        _n = _rs2.cleanup_orphan_projects()
        if _n:
            logger.info(f'Đã dọn {_n} thư mục dự án review mồ côi.')
    except Exception as _e:
        logger.warning(f'Dọn thư mục review mồ côi lỗi (bỏ qua): {_e}')
    try:
        from services import du_an_mau as _dm
        from services.config_service import get_config_service as _cfg
        if _dm.khoi_phuc_video(_cfg().get_review_root()):
            logger.info('Đã chép lại video mẫu cho các dự án mẫu cũ.')
    except Exception as _e:
        logger.warning(f'Khôi phục video mẫu lỗi (bỏ qua): {_e}')
    yield
    logger.info('[GPU] Backend dang tat — giai phong VRAM...')
    _release_all_gpu_models()


app = FastAPI(title='SubForge API', description='Hệ thống Backend cho SubForge Review',
              lifespan=_app_lifespan)


@app.middleware('http')
async def app_lang_middleware(request: Request, call_next):
    from utils.i18n import set_lang
    set_lang(request.headers.get('x-app-lang'))
    return await call_next(request)


@app.middleware('http')
async def catch_exceptions_middleware(request: Request, call_next):
    try:
        return await call_next(request)
    except Exception as e:
        logger.error(f'[RUNTIME ERROR] {str(e)}')
        log_crash(e)
        raise e


app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])
from urllib.parse import urlparse
from fastapi.responses import JSONResponse
_LOCAL_HOSTNAMES = {'localhost', '127.0.0.1', '::1'}


def _host_is_local(host_header: str) -> bool:
    if not host_header:
        return False
    h = host_header.strip().lower()
    if h.startswith('['):
        h = h.split(']', 1)[0].lstrip('[')
    else:
        h = h.split(':', 1)[0]
    return h in _LOCAL_HOSTNAMES


def _origin_is_allowed(origin: str) -> bool:
    if not origin or origin == 'null':
        return True
    try:
        parsed = urlparse(origin)
    except Exception:
        return False
    if parsed.scheme == 'file':
        return True
    return (parsed.hostname or '').lower() in _LOCAL_HOSTNAMES


@app.middleware('http')
async def local_only_guard(request: Request, call_next):
    if not _host_is_local(request.headers.get('host', '')):
        logger.warning(f"[SECURITY] Chặn request với Host lạ: {request.headers.get('host')}")
        return JSONResponse(status_code=403, content={'detail': 'Forbidden: invalid host.'})
    if not _origin_is_allowed(request.headers.get('origin')):
        logger.warning(f"[SECURITY] Chặn request từ Origin lạ: {request.headers.get('origin')}")
        return JSONResponse(status_code=403, content={'detail': 'Forbidden: cross-origin request blocked.'})
    return await call_next(request)


from models.database import init_db, SessionLocal
from routes import video_routes, review_routes, hardware_routes, auth_routes, tts_routes, compositor_preset_routes, model_routes, storage_routes
from routes.video_routes import get_data_root
logger.info('[DB] Đang khởi tạo kết nối cơ sở dữ liệu...')
init_db()
logger.info('[DB] Kết nối Database thành công.')
from models.database import get_app_data_dir, get_bootstrap_dir


def initialize_static_mounts():
    '''Hàm mount các thư mục tĩnh, có kiểm tra lỗi để tránh treo App'''
    db = SessionLocal()
    try:
        logger.info('[MOUNT] Đang truy vấn đường dẫn Data Root...')
        root_path = get_data_root(db)
        if root_path and os.path.exists(root_path):
            app.mount('/data', StaticFiles(directory=root_path), name='data')
            logger.info(f'[MOUNT] Đã kết nối thư mục Data: {root_path}')
        else:
            logger.warning(f'[MOUNT] Thư mục Data không tồn tại hoặc chưa cấu hình: {root_path}')
    except Exception as e:
        logger.error(f'[MOUNT] Không thể mount thư mục Data: {e}')
    finally:
        db.close()
    try:
        from services.config_service import get_config_service
        review_dir = os.path.abspath(get_config_service().get_review_root())
        if not os.path.exists(review_dir):
            os.makedirs(review_dir, exist_ok=True)
            logger.info(f'[SYSTEM] Đã tạo mới thư mục lưu trữ: {review_dir}')
        app.mount('/review_projects', StaticFiles(directory=review_dir), name='review_projects')
        logger.info(f'[MOUNT] Đã kết nối lưu trữ Review: {review_dir}')
    except Exception as e:
        logger.error(f'[MOUNT] Lỗi nghiêm trọng khi tạo thư mục làm việc: {e}')


initialize_static_mounts()
app.include_router(video_routes.router)
app.include_router(review_routes.router)
from routes import review_advanced_routes
from routes import batch_routes
app.include_router(review_advanced_routes.router)
from routes import sub_track_routes
for _r in sub_track_routes.routers():
    app.include_router(_r)
app.include_router(batch_routes.router)
from routes import nhanh_routes
app.include_router(nhanh_routes.router)
app.include_router(hardware_routes.router)
app.include_router(auth_routes.router)
app.include_router(tts_routes.router)
app.include_router(compositor_preset_routes.router)
app.include_router(model_routes.router)
app.include_router(storage_routes.router)
from routes import youtube_folder_routes, youtube_meta_routes, youtube_routes
app.include_router(youtube_routes.router)
app.include_router(youtube_meta_routes.router)
app.include_router(youtube_folder_routes.router)
from routes import sangtao_routes
app.include_router(sangtao_routes.router)
LAST_HEARTBEAT = time.time()


@app.post('/api/heartbeat')
async def receive_heartbeat():
    global LAST_HEARTBEAT
    LAST_HEARTBEAT = time.time()
    return {'status': 'ok'}


@app.get('/debug/status')
async def debug_status():
    return {'status': 'online', 'engine': 'google-tier-3-flash',
            'ffmpeg_status': 'ok' if os.path.exists(ffmpeg_exe) else 'error'}


try:
    dist_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '../frontend/dist'))
    if os.path.exists(dist_path):
        app.mount('/', StaticFiles(directory=dist_path, html=True), name='frontend')
        logger.info(f'[MOUNT] Đã phục vụ Frontend build tại: {dist_path}')
    else:
        logger.warning(f'[MOUNT] Thư mục Frontend build không tìm thấy tại: {dist_path}')
except Exception as e:
    logger.error(f'[MOUNT] Lỗi mount Frontend: {e}')


def check_heartbeat():
    '''Canh app còn sống (xem utils/canh_app.py). Chỉ còn vòng lặp + tự tắt; logic nằm ở CanhApp để test riêng.'''
    global LAST_HEARTBEAT
    from utils.canh_app import CanhApp
    canh = CanhApp(_dang_co_viec_nang)
    time.sleep(30)
    while True:
        time.sleep(5)
        quyet_dinh, LAST_HEARTBEAT = canh.mot_vong(time.time(), LAST_HEARTBEAT)
        if quyet_dinh == 'tat':
            os.kill(os.getpid(), signal.SIGTERM)
            return


def _dang_co_viec_nang() -> bool:
    '''Máy đang chạy việc nặng (hàng đợi việc nặng, lô, hay xuất bước 2/2) không.'''
    try:
        from services import work_queue as WQ
        if WQ.viec_dang_chay():
            return True
    except Exception:
        pass
    try:
        from services import batch_store as BS
        if any(BS.is_running(b) for b in BS.list_all()):
            return True
    except Exception:
        pass
    try:
        from routes.review_routes import dang_xuat_compositor
        if dang_xuat_compositor():
            return True
    except Exception:
        pass
    return False


def _cong_uu_tien() -> int:
    try:
        p = int(os.environ.get('SUBFORGE_PREFERRED_PORT') or 0)
        if 8000 <= p <= 8020:
            return p
    except ValueError:
        pass
    return 8000


def find_free_port(preferred=8000, fallback_range=(8001, 8020)):
    '''Thử cổng ưu tiên 8000 trước, nếu bị chiếm thì tìm cổng tiếp theo trong range.'''
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(('127.0.0.1', preferred))
            return preferred
        except OSError:
            logger.warning(f'[PORT] Cổng {preferred} đã bị chiếm. Đang tìm cổng thay thế...')
    for port in range(fallback_range[0], fallback_range[1] + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(('127.0.0.1', port))
                logger.warning(f'[PORT] ⚠ Sử dụng cổng thay thế: {port} (cổng 8000 đã bị chiếm)')
                return port
            except OSError:
                continue
    return preferred


if __name__ == '__main__':
    threading.Thread(target=check_heartbeat, daemon=True).start()
    port = find_free_port(preferred=_cong_uu_tien())
    app_dir = get_bootstrap_dir()
    os.makedirs(app_dir, exist_ok=True)
    port_file = os.path.join(app_dir, 'port.json')
    try:
        with open(port_file, 'w') as f:
            json.dump({'port': port, 'host': '127.0.0.1'}, f)
        logger.info(f'[PORT] Đã ghi port.json: {port_file}')
    except Exception as e:
        logger.error(f'Lỗi ghi port: {e}')
    if not os.environ.get('SUBFORGE_ADMIN_URL') and not os.environ.get('ADMIN_URL'):
        from services.config_service import get_config_service
        _cfg = get_config_service()
        if not _cfg.get_admin_url():
            _default_admin = 'http://localhost:3000'
            _cfg.update_auth_config(admin_url=_default_admin)
            logger.info(f'[CONFIG] Đã tự động thiết lập admin_url = {_default_admin}')
    logger.info(f'[SERVER] Backend đang khởi động tại http://localhost:{port}')
    from utils.dev_mode import is_dev_mode
    if is_dev_mode():
        logger.info('[AUTH] Chế độ DEV — bỏ qua kiểm tra bản quyền trên Admin Portal')
    if os.environ.get('SUBFORGE_DESKTOP') != 'true' and not getattr(sys, 'frozen', False):
        def open_browser():
            time.sleep(1.5)
            webbrowser.open(f'http://localhost:{port}')
        threading.Thread(target=open_browser, daemon=True).start()
    is_packaged = getattr(sys, 'frozen', False)
    if is_packaged:
        uvicorn.run(app, host='127.0.0.1', port=port)
    else:
        uvicorn.run('main:app', host='127.0.0.1', port=port, reload=True)
