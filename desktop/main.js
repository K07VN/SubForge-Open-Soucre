const {
    app,
    BrowserWindow,
    Menu,
    shell,
    ipcMain,
    dialog,
    session,
    nativeTheme,
    powerMonitor,
} = require("electron");
const http = require("http");
const path = require("path");
const { spawn, execFileSync } = require("child_process");
const fs = require("fs");
const LOCAL_MODE = process.env.SUBFORGE_LOCAL_MODE === "1";

// ─── DEPLOYMENT CONFIG ─────────────────────────────────────────────────────
// Sửa file electron/deploy.config.js để thay đổi URL Admin Portal trước khi build
const {
    ADMIN_URL,
    STAGING_ADMIN_URL,
    PREFERRED_PY_PORT,
    DESKTOP_SECRET,
    GPU_PACK_URL,
    GPU_PACK_SHA256,
    GPU_PACK_VERSION,
    DEEPSEEK_API_KEY,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODELS,
    DEEPSEEK_DEFAULT_MODEL,
    GEMINI_API_KEY,
    GEMINI_BASE_URL,
    GEMINI_MODELS,
    GEMINI_DEFAULT_MODEL,
} = require("./deploy.config");

// ─── BẢO MẬT: CHẶN CỜ GỠ LỖI CỦA CHROMIUM/NODE ─────────────────────────────
// `--remote-debugging-port` mở TOÀN QUYỀN Chrome DevTools Protocol vào renderer
// (đọc/sửa DOM, eval JS tuỳ ý, gọi window.electronAPI) — mạnh hơn F12 rất nhiều
// và F12 thì đã bị chặn ở bản đóng gói, nên bỏ ngỏ cờ này khiến việc chặn F12
// gần như vô nghĩa. Chromium tự parse cờ TRƯỚC khi code app chạy nên phải kiểm
// tra argv và thoát ngay tại đây (trước app 'ready', trước khi tạo cửa sổ).
if (app.isPackaged) {
    const FORBIDDEN_SWITCHES = [
        "--remote-debugging-port",
        "--remote-debugging-pipe",
        "--remote-allow-origins",
        "--inspect",
        "--inspect-brk",
        "--inspect-port",
        "--js-flags",
        "--gpu-launcher",
        "--utility-cmd-prefix",
        "--renderer-cmd-prefix",
    ];
    const offending = process.argv
        .slice(1)
        .find((arg) =>
            FORBIDDEN_SWITCHES.some(
                (sw) => arg === sw || arg.startsWith(`${sw}=`),
            ),
        );
    if (offending) {
        // Không dùng log() — logStream chưa khởi tạo ở thời điểm này.
        console.error(
            `[SECURITY] Từ chối khởi động: phát hiện cờ gỡ lỗi '${offending}'.`,
        );
        app.exit(1);
    }
}

// URL Admin Portal dùng thực tế:
//   - Dev (chưa đóng gói): env SUBFORGE_ADMIN_URL (nếu có) → localhost:3000
//   - Bản đóng gói:        STAGING_ADMIN_URL (nếu build bản thử) → ADMIN_URL
//
// BẢO MẬT: bản đóng gói KHÔNG còn tôn trọng env SUBFORGE_ADMIN_URL. Trước đây env
// thắng cả bản phát hành, nên `set SUBFORGE_ADMIN_URL=http://127.0.0.1:9999` + một
// HTTP server 10 dòng trả {"valid":true} là mở khoá bản quyền mà không sửa file nào
// (backend tin tuyệt đối vào câu trả lời của "portal"). Dev vẫn override được vì
// khi chạy từ source thì !app.isPackaged; còn muốn ép BẢN ĐÓNG GÓI trỏ sang portal
// thử thì khai STAGING_ADMIN_URL trong deploy.config.js rồi build lại (giá trị được
// nướng vào bản build → người dùng cuối không đổi được lúc chạy).
const RESOLVED_ADMIN_URL = app.isPackaged
    ? STAGING_ADMIN_URL || ADMIN_URL
    : process.env.SUBFORGE_ADMIN_URL || "http://localhost:3000";

/**
 * Env truyền xuống backend Python. Các biến BẢO MẬT được ghi đè SAU `...process.env`
 * để biến do người dùng đặt ở cấp hệ thống không lọt xuống backend.
 * (Electron kế thừa toàn bộ env của người dùng — đó là đường crack dễ nhất trước đây.)
 */
function securityHardenedEnv(extra) {
    const base = { ...process.env, ...extra };
    if (!app.isPackaged) return base;
    return {
        ...base,
        // Vô hiệu hoá mọi công tắc "bỏ qua bản quyền" trong bản phát hành.
        // dev_mode.py cũng đã tự chặn khi sys.frozen — đây là lớp thứ hai (defense
        // in depth): kể cả ai patch được dev_mode.py thì cũng phải patch cả chỗ này.
        SUBFORGE_FORCE_LICENSE_VERIFY: "1",
        SUBFORGE_SKIP_LICENSE_VERIFY: "",
        SUBFORGE_DEV: "",
        // Chặn nới hạn cache license thành vĩnh viễn (env này vốn đọc tự do).
        SUBFORGE_LICENSE_CACHE_SECS: "1800",
    };
}
// ───────────────────────────────────────────────────────────────────────────

// TÁCH RIÊNG dev ↔ bản cài: userData/bootstrap của DEV nằm ở %APPDATA%\SubForge-Dev,
// bản đóng gói ở %APPDATA%\SubForge. Trước đây dùng CHUNG (app.name = "subforge" từ
// package.json) nên cache/log/port.json/con trỏ data root giẫm lên nhau. PHẢI đặt
// TRƯỚC mọi app.getPath("userData") (vd LOG_PATH bên dưới) và trước app 'ready'.
// Tên hiển thị để nhất quán với productName.
app.setName("SubForge Pro");
const APP_DATA_FOLDER = app.isPackaged ? "SubForge" : "SubForge-Dev";
app.setPath(
    "userData",
    path.join(app.getPath("appData"), APP_DATA_FOLDER),
);
// ───────────────────────────────────────────────────────────────────────────

let mainWindow = null;
let pyProc = null;
let versionCheckInterval = null;

//  BACKEND THOÁT NGOÀI Ý MUỐN (08-10-2026) — xem xuLyBackendThoat(). Trước đây backend chết (sập, tự tắt vì mất
//  heartbeat sau khi máy ngủ) là giao diện im lặng lỗi từng nút, người dùng phải tắt app bật lại.
let tatBackendChuDong = false;            // đang tự tay tắt (thoát app / khởi động lại) -> 'close' không phải sự cố
let lanTuKhoiDongLai = [];                // mốc các lần tự khởi động lại gần đây — chặn vòng lặp sập liên tục
const TU_KHOI_DONG_LAI_TOI_DA = 3;        // tối đa 3 lần trong 10 phút, quá thì để người dùng quyết
const TU_KHOI_DONG_LAI_CUA_SO_MS = 10 * 60 * 1000;
let congBackendMongMuon = null;           // cổng lần trước — khởi động lại cố giữ nguyên để không phải tạo lại cửa sổ

// Backend (Python) ghi port.json vào thư mục bootstrap (get_bootstrap_dir) = ĐÚNG
// userData đã đặt ở trên (SubForge cho bản cài, SubForge-Dev cho dev). Đọc đúng chỗ
// đó, tránh đọc nhầm cổng -> cửa sổ trắng khi backend fallback sang cổng khác.
function getBackendPortFile() {
    return path.join(app.getPath("userData"), "port.json");
}

/** Dọn process Python/backend zombie còn chiếm cổng 8000–8020 (giữ VRAM GPU). */
function killStaleBackendPorts() {
    if (LOCAL_MODE) return;
    if (process.platform !== "win32") return;
    const basePort = PREFERRED_PY_PORT || 8000;
    let out;
    try {
        out = execFileSync("netstat", ["-ano"], { encoding: "utf8" });
    } catch (e) {
        log(`[PORT] Không quét được netstat: ${e.message}`);
        return;
    }
    const killed = new Set();
    // CHỈ kill tiến trình đúng danh tính backend — cổng trong dải có thể đang được
    // app KHÁC của người dùng dùng (dev server...), kill mù theo cổng là giết nhầm.
    const BACKEND_IMAGES = new Set([
        "subforge_backend.exe",
        "subforge_backend_gpu.exe",
        "python.exe",
        "pythonw.exe",
    ]);
    const pidImageName = (pid) => {
        try {
            const csv = execFileSync(
                "tasklist",
                ["/fi", `PID eq ${pid}`, "/fo", "csv", "/nh"],
                { encoding: "utf8", timeout: 5000 },
            );
            const m = csv.match(/^"([^"]+)"/m);
            return m ? m[1].toLowerCase() : "";
        } catch (_) {
            return "";
        }
    };
    // +20 khớp find_free_port fallback_range=(8001, 8020) của backend/main.py —
    // trước đây chỉ quét +10 nên zombie bind 8011-8020 thoát lưới dọn vĩnh viễn.
    for (let port = basePort; port <= basePort + 20; port++) {
        const portRe = new RegExp(
            `(?:127\\.0\\.0\\.1|0\\.0\\.0\\.0|\\[::1\\]):${port}\\s`,
        );
        for (const line of out.split("\n")) {
            if (!line.includes("LISTENING") || !portRe.test(line)) continue;
            const parts = line.trim().split(/\s+/);
            const pid = parts[parts.length - 1];
            if (!pid || !/^\d+$/.test(pid) || pid === "0" || killed.has(pid))
                continue;
            const image = pidImageName(pid);
            if (!BACKEND_IMAGES.has(image)) {
                log(
                    `[PORT] Bỏ qua PID ${pid} trên cổng ${port} ('${image || "?"}' không phải backend).`,
                );
                continue;
            }
            try {
                execFileSync("taskkill", ["/f", "/t", "/pid", String(pid)], {
                    stdio: "ignore",
                    timeout: 5000,
                });
                killed.add(pid);
                log(`[PORT] Đã dọn process cũ trên cổng ${port} (PID ${pid})`);
            } catch (_) {
                /* process có thể đã tắt */
            }
        }
    }
    // Backstop QUAN TRỌNG: dọn các python worker multiprocessing MỒ CÔI.
    killOrphanBackendWorkers();
}

/** Giết các python worker `multiprocessing.spawn` bị MỒ CÔI (tiến trình cha không
 * còn sống). Khi render (demucs/TTS) sinh worker rồi backend cha bị kill trước, các
 * worker này sống sót và GIỮ handle socket kế thừa của cổng 8000 -> netstat gán
 * socket cho PID cha ĐÃ CHẾT nên kill-theo-cổng ở trên không trúng worker thật.
 * Chỉ giết worker CÓ cha đã chết -> không đụng worker của backend đang chạy hợp lệ
 * hay tiến trình python khác (VS Code language server không dùng multiprocessing). */
function killOrphanBackendWorkers() {
    if (LOCAL_MODE) return;
    if (process.platform !== "win32") return;
    // PowerShell: liệt kê python.exe chạy 'multiprocessing', với cha KHÔNG còn sống
    // thì Stop-Process. Dùng Get-CimInstance (wmic đã bị gỡ trên Windows 11 mới).
    const ps =
        "$ErrorActionPreference='SilentlyContinue';" +
        "$alive=@{};(Get-Process).Id|ForEach-Object{$alive[[int]$_]=$true};" +
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe' OR Name='pythonw.exe'\"|" +
        "Where-Object{$_.CommandLine -match 'multiprocessing'}|" +
        "ForEach-Object{ if(-not $alive[[int]$_.ParentProcessId]){ " +
        "Stop-Process -Id $_.ProcessId -Force; Write-Output $_.ProcessId } }";
    try {
        const out = execFileSync(
            "powershell",
            ["-NoProfile", "-NonInteractive", "-Command", ps],
            { encoding: "utf8", timeout: 8000 },
        );
        const killedPids = (out || "").trim();
        if (killedPids) {
            log(`[PORT] Đã dọn python worker mồ côi (giữ cổng): ${killedPids.replace(/\s+/g, ", ")}`);
        }
    } catch (e) {
        log(`[PORT] Dọn worker mồ côi gặp lỗi: ${e.message}`);
    }
}

// Đọc port backend thực tế từ file port.json (backend ghi sau khi khởi động)
function readBackendPort() {
    if (LOCAL_MODE) return Number(process.env.SUBFORGE_LOCAL_PORT || 18080);
    try {
        const portFile = getBackendPortFile();
        if (fs.existsSync(portFile)) {
            const data = JSON.parse(fs.readFileSync(portFile, "utf8"));
            return data.port || PREFERRED_PY_PORT;
        }
    } catch (e) {
        log(`[PORT] Không đọc được port.json: ${e.message}`);
    }
    return PREFERRED_PY_PORT;
}

//  Backend có đang chạy việc nặng không (hàng đợi việc nặng + lô chạy hàng loạt).
//  Trả về ``null`` khi rảnh, hoặc mô tả để hiện trong hộp xác nhận lúc đóng app.
async function hoiViecDangChay() {
    const port = readBackendPort();
    //  Chờ TỐI ĐA 1,5 giây: đóng app là thao tác người dùng mong "ăn liền", treo
    //  cửa sổ chỉ vì một lượt gọi API là mất kiên nhẫn ngay.
    const ctrl = new AbortController();
    const hen = setTimeout(() => ctrl.abort(), 1500);
    try {
        const res = await fetch(`http://127.0.0.1:${port}/api/batch/queue/status`, {
            signal: ctrl.signal,
        });
        if (!res.ok) return null;
        const d = await res.json();
        const dangChay = d.dang_chay || [];
        const cho = d.cho || [];
        const lo = d.lo || null;
        if (!dangChay.length && !cho.length && !lo) return null;

        const dong = [];
        if (dangChay.length) dong.push(`Đang chạy: ${dangChay[0].nhan}`);
        if (lo) {
            dong.push(
                `Lô "${lo.title}": ${lo.step_label} — xong ${lo.xong}/${lo.tong}` +
                (lo.dang_lam ? ` (đang làm: ${lo.dang_lam})` : "")
            );
        }
        if (cho.length) dong.push(`${cho.length} việc khác đang xếp hàng`);
        dong.push("");
        dong.push(
            "Đóng app bây giờ sẽ DỪNG NGANG việc đang chạy; phần dở phải chạy lại từ đầu."
        );
        return {
            tieu_de: lo ? "Lô chạy hàng loạt đang chạy" : "Máy đang chạy việc nặng",
            chi_tiet: dong.join("\n"),
        };
    } finally {
        clearTimeout(hen);
    }
}

// Đường dẫn lưu file log + XOAY VÒNG (rotation) để không phình vô hạn.
// File này gom cả stdout/stderr của backend (rất nhiều dòng: mỗi câu STT, tiến độ
// dịch...). Mở kiểu append nên nếu không giới hạn sẽ tích luỹ qua từng phiên tới
// hàng trăm MB. Cơ chế: giữ 1 bản backup (.1); vượt LOG_MAX_BYTES thì xoay vòng —
// lúc mở app (dồn qua nhiều phiên) và cả giữa phiên (job dài, log dày).
const LOG_PATH = path.join(app.getPath("userData"), "subforge_desktop.log");
// userData có thể CHƯA tồn tại (lần chạy đầu / thư mục bị xoá) — createWriteStream
// không tự tạo thư mục cha, stream sẽ bắn 'error' ENOENT không ai bắt -> Electron
// bung dialog "JavaScript error". Phải mkdir trước khi mở log.
try {
    fs.mkdirSync(app.getPath("userData"), { recursive: true });
} catch (e) {
    /* ignore */
}
const LOG_ROTATED = LOG_PATH + ".1";
const LOG_MAX_BYTES = 5 * 1024 * 1024; // 5MB/file -> tối đa ~10MB (hiện tại + backup)

function rotateLogFiles() {
    // Xoá backup cũ rồi đổi tên file hiện tại thành .1 (chỉ giữ 1 đời backup).
    // Trả về true/false để caller biết rename có thành công không — nếu fail
    // (file bị AV/tail giữ khoá trên Windows) thì KHÔNG được reset bộ đếm,
    // tránh cap 5MB bị re-arm trên chính file cũ -> phình vô hạn.
    try {
        if (fs.existsSync(LOG_ROTATED)) fs.unlinkSync(LOG_ROTATED);
        if (fs.existsSync(LOG_PATH)) fs.renameSync(LOG_PATH, LOG_ROTATED);
        return true;
    } catch (e) {
        return false;
    }
}

// Xoay NGAY lúc mở app nếu file tích luỹ từ các phiên trước đã quá lớn.
try {
    if (
        fs.existsSync(LOG_PATH) &&
        fs.statSync(LOG_PATH).size >= LOG_MAX_BYTES
    ) {
        rotateLogFiles();
    }
} catch (e) {
    /* ignore */
}

// Gắn handler 'error' để mọi lỗi I/O của stream log (đĩa đầy, file bị khoá,
// ENOENT...) không trở thành uncaught exception làm sập app — mất log thì
// chấp nhận được, sập app thì không.
function openLogStream() {
    const s = fs.createWriteStream(LOG_PATH, { flags: "a" });
    s.on("error", () => {});
    return s;
}

let logStream = openLogStream();
let logBytes = (() => {
    try {
        return fs.existsSync(LOG_PATH) ? fs.statSync(LOG_PATH).size : 0;
    } catch (e) {
        return 0;
    }
})();
let logRotating = false;
// Cờ đánh dấu logStream đã đóng (khi thoát app). Sau khi đóng, tiến trình con
// vẫn có thể phát sự kiện 'close'/'data' và gọi log() -> phải chặn ghi để tránh
// ERR_STREAM_WRITE_AFTER_END (Electron bung dialog "JavaScript error" khi tắt).
let logStreamEnded = false;

// Xoay vòng GIỮA PHIÊN: đóng hẳn stream cũ (chờ flush) rồi mới rename để tránh
// EPERM do file còn handle mở trên Windows; sau đó mở lại file mới.
function rotateLiveLog() {
    if (logRotating || logStreamEnded) return;
    logRotating = true;
    // Chốt an toàn: nếu callback của end() không bao giờ chạy (stream đã bị huỷ
    // bởi luồng thoát app chạy đua), tự nhả cờ sau 5s để log() không câm lặng
    // suốt phần còn lại của phiên.
    const failsafe = setTimeout(() => {
        logRotating = false;
    }, 5000);
    const old = logStream;
    try {
        old.end(() => {
            clearTimeout(failsafe);
            const rotated = rotateLogFiles();
            if (!logStreamEnded) {
                try {
                    logStream = openLogStream();
                    if (rotated) {
                        logBytes = 0;
                    } else {
                        // Rename fail (file bị khoá): vẫn append vào file cũ nhưng
                        // KHÔNG reset đếm — lùi 1MB để thử xoay lại sau ~1MB nữa
                        // thay vì mỗi dòng (đỡ spam) mà cũng không đợi thêm 5MB.
                        logBytes = Math.max(0, LOG_MAX_BYTES - 1024 * 1024);
                    }
                } catch (e) {
                    /* ignore */
                }
            }
            logRotating = false;
        });
    } catch (e) {
        clearTimeout(failsafe);
        logRotating = false;
    }
}

// Data root (nơi chứa gpu_pack / ai_models / dự án) — người dùng có thể đổi trong
// Cài đặt; backend ghi lựa chọn vào %APPDATA%/SubForge/storage.json. Electron PHẢI
// đọc file này TRƯỚC khi spawn backend (quyết định exe GPU nào chạy) nên đọc động
// mỗi lần thay vì hằng số.
const DEFAULT_WORKSTATION_DIR = app.isPackaged
    ? "C:/SubForge_Workstation_Data"
    : "C:/SubForge_Workstation_Data_Dev";

function getStorageConfigFile() {
    // userData đã tách theo chế độ (SubForge vs SubForge-Dev) nên chỉ cần storage.json
    // trong đó — dev và bản cài tự động độc lập. MIRROR backend storage_config_path().
    return path.join(app.getPath("userData"), "storage.json");
}

function getWorkstationDir() {
    try {
        const raw = fs.readFileSync(getStorageConfigFile(), "utf8");
        const root = String(JSON.parse(raw).data_root || "").trim();
        if (root) {
            // TỰ CHỮA: bản ĐÓNG GÓI tuyệt đối không dùng thư mục "_Dev" (di sản từ
            // file storage.json cũ dùng chung với dev) — bỏ qua, dùng mặc định.
            if (app.isPackaged && /_Dev$/i.test(root.replace(/[\\/]+$/, ""))) {
                log(`[STORAGE] Bản cài bỏ qua data root dev '${root}' — dùng mặc định.`);
                return DEFAULT_WORKSTATION_DIR;
            }
            // Ổ đĩa đã cấu hình không còn (ổ rời bị rút) -> lui về mặc định.
            const drive = path.parse(root).root;
            if (!drive || fs.existsSync(drive)) return root;
            log(`[STORAGE] Ổ đĩa của data root '${root}' không tồn tại — dùng mặc định.`);
        }
    } catch (_) {
        /* chưa có storage.json -> mặc định */
    }
    return DEFAULT_WORKSTATION_DIR;
}

function getGpuPackExe() {
    return path.join(
        getWorkstationDir(),
        "gpu_pack",
        "subforge_backend_gpu",
        "subforge_backend_gpu.exe",
    );
}
function getGpuUninstallFlag() {
    return path.join(getWorkstationDir(), "uninstall_gpu.flag");
}
function getForceCpuFlag() {
    return path.join(getWorkstationDir(), "force_cpu.flag");
}
const MIN_GPU_EXE_BYTES = 5 * 1024 * 1024;

function hasNvidiaGpu() {
    if (process.platform !== "win32") return false;

    // 1. Try nvidia-smi command (fastest check if CUDA is path-configured)
    try {
        execFileSync("nvidia-smi", { stdio: "ignore", windowsHide: true });
        return true;
    } catch (_) {}

    // 2. Try common nvidia-smi path
    try {
        const defaultWinPath =
            "C:\\Program Files\\NVIDIA Corporation\\NVSMI\\nvidia-smi.exe";
        if (fs.existsSync(defaultWinPath)) {
            return true;
        }
    } catch (_) {}

    // 3. Try wmic (Windows legacy tool)
    try {
        const out = execFileSync(
            "wmic",
            ["path", "win32_VideoController", "get", "name"],
            { encoding: "utf8", timeout: 4000, windowsHide: true },
        );
        if (/nvidia/i.test(out)) return true;
    } catch (_) {}

    // 4. Try powershell (robust modern Windows tool)
    try {
        const cmd =
            "Get-CimInstance Win32_VideoController | Select-Object Name | ConvertTo-Json";
        const out = execFileSync(
            "powershell",
            ["-NoProfile", "-NonInteractive", "-Command", cmd],
            { encoding: "utf8", timeout: 8000, windowsHide: true },
        );
        if (/nvidia/i.test(out)) return true;
    } catch (_) {}

    return false;
}

function isValidGpuBackend(exePath) {
    try {
        return (
            fs.existsSync(exePath) &&
            fs.statSync(exePath).size >= MIN_GPU_EXE_BYTES
        );
    } catch {
        return false;
    }
}

/** Ưu tiên backend CUDA đóng gói sẵn trong app, fallback CPU. */
function resolvePackagedBackendExe() {
    const cpuExe = path.join(
        process.resourcesPath,
        "backend_dist",
        "subforge_backend",
        "subforge_backend.exe",
    );

    if (fs.existsSync(getGpuUninstallFlag()) || fs.existsSync(getForceCpuFlag())) {
        const reason = fs.existsSync(getGpuUninstallFlag())
            ? "gỡ cài đặt GPU"
            : "chuyển sang CPU";
        log(`[GPU] Chế độ CPU only (${reason}).`);
        return { exe: cpuExe, mode: "cpu", gpuBundled: false };
    }

    const nvidia = hasNvidiaGpu();
    const gpuPackInstalled = isValidGpuBackend(getGpuPackExe());

    if (nvidia && gpuPackInstalled) {
        log(
            "[GPU] Phát hiện GPU NVIDIA và gói CUDA Pack rời cục bộ. Khởi chạy ở chế độ CUDA.",
        );
        return { exe: cpuExe, mode: "cuda", gpuBundled: false };
    }

    if (nvidia) {
        log(
            "[GPU] Có GPU NVIDIA nhưng chưa cài gói CUDA Pack — chạy chế độ CPU.",
        );
    } else {
        log("[GPU] Không phát hiện GPU NVIDIA — chạy chế độ CPU.");
    }

    return { exe: cpuExe, mode: "cpu", gpuBundled: false };
}

function log(msg) {
    const time = new Date().toISOString();
    const line = `[${time}] ${msg}`;
    // Trong lúc đang xoay vòng (đóng/mở lại stream), chỉ ghi console để tránh ghi
    // vào stream cũ đang .end() — vài mili-giây, không mất mát đáng kể.
    if (!logStreamEnded && !logRotating && logStream.writable) {
        try {
            logStream.write(`${line}\n`);
            logBytes += Buffer.byteLength(line, "utf8") + 1;
            if (logBytes >= LOG_MAX_BYTES) rotateLiveLog();
        } catch (e) {
            /* stream đang đóng -> chỉ ghi ra console */
        }
    }
    console.log(line);
}

// Khởi chạy FastAPI Backend dưới dạng Sidecar Process
function startPythonSubprocess() {
    tatBackendChuDong = false;
    if (LOCAL_MODE) {
        log("[LOCAL] Dùng backend do run_local.py quản lý.");
        return;
    }
    if (app.isPackaged) {
        const {
            exe: backendExePath,
            mode,
            gpuBundled,
        } = resolvePackagedBackendExe();
        log(
            `Khởi chạy Packaged Backend [${mode.toUpperCase()}] tại: ${backendExePath}`,
        );

        pyProc = spawn(backendExePath, [], {
            cwd: path.dirname(backendExePath),
            env: securityHardenedEnv({
                SUBFORGE_DESKTOP: "true",
                // Backend canh tiến trình này còn sống để không tự tắt oan (utils/canh_app.py); cổng muốn giữ sau restart.
                SUBFORGE_PARENT_PID: String(process.pid),
                SUBFORGE_PREFERRED_PORT: congBackendMongMuon ? String(congBackendMongMuon) : "",
                // Phiên bản app: nguồn tin cậy DUY NHẤT cho mọi thứ backend gửi lên
                // server (xác thực, thống kê, log lỗi). Không có biến này thì backend
                // phải đoán theo header của frontend và rơi về "1.0.0".
                SUBFORGE_APP_VERSION: app.getVersion(),
                SUBFORGE_ADMIN_URL: RESOLVED_ADMIN_URL,
                SUBFORGE_DESKTOP_SECRET: DESKTOP_SECRET,
                SUBFORGE_BACKEND_MODE: mode,
                SUBFORGE_GPU_BUNDLED: gpuBundled ? "true" : "false",
                // Data root người dùng chọn (storage.json) — backend ưu tiên env này.
                SUBFORGE_DATA_ROOT: getWorkstationDir(),
                // Model AI đóng gói qua extraResources -> resourcesPath/ai_models
                // Chỉ truyền nếu thư mục này tồn tại và không rỗng (được đóng gói sẵn model).
                // Nếu rỗng, để trống để backend dùng thư mục dữ liệu ngoài (Workstation_Data) giúp giữ model khi nâng cấp.
                SUBFORGE_MODELS_DIR: (() => {
                    const p = path.join(process.resourcesPath, "ai_models");
                    try {
                        if (fs.existsSync(p) && fs.readdirSync(p).length > 0) {
                            return p;
                        }
                    } catch (e) {}
                    return "";
                })(),
                // URL/SHA gói GPU — khai 1 nơi ở deploy.config.js (không hardcode trong Python)
                SUBFORGE_GPU_PACK_URL: GPU_PACK_URL || "",
                SUBFORGE_GPU_PACK_SHA256: GPU_PACK_SHA256 || "",
                // Phiên bản GÓI GPU (tách khỏi phiên bản app) — để phát hiện gói cũ khi
                // nâng cấp app. Nhiều bản app dùng chung 1 gói thì giữ nguyên giá trị này.
                SUBFORGE_GPU_PACK_VERSION: GPU_PACK_VERSION || "",
                // DeepSeek — base URL + danh sách model khai ở deploy.config.js.
                // API key mặc định thường TRỐNG (người dùng tự nhập trong Cài đặt).
                SUBFORGE_DEEPSEEK_API_KEY: DEEPSEEK_API_KEY || "",
                SUBFORGE_DEEPSEEK_BASE_URL:
                    DEEPSEEK_BASE_URL || "https://api.deepseek.com",
                SUBFORGE_DEEPSEEK_MODELS: (DEEPSEEK_MODELS || []).join(","),
                SUBFORGE_DEEPSEEK_DEFAULT_MODEL: DEEPSEEK_DEFAULT_MODEL || "",
                // Gemini (AI Studio) — bộ AI thứ hai, cùng cách khai như DeepSeek.
                SUBFORGE_GEMINI_API_KEY: GEMINI_API_KEY || "",
                SUBFORGE_GEMINI_BASE_URL: GEMINI_BASE_URL || "",
                SUBFORGE_GEMINI_MODELS: (GEMINI_MODELS || []).join(","),
                SUBFORGE_GEMINI_DEFAULT_MODEL: GEMINI_DEFAULT_MODEL || "",
            }),
        });
    } else {
        // Môi trường Development: Chạy trực tiếp backend bằng Python ảo (.venv)
        // nếu có, nếu không thì dùng py launcher (tránh App Execution Alias).
        const backendPath = path.join(__dirname, "../backend/main.py");
        const venvPython = path.join(
            __dirname,
            "../backend/.venv/Scripts/python.exe",
        );
        const hasVenv = fs.existsSync(venvPython);
        const pythonBin = hasVenv ? venvPython : "py";
        log(
            `Khởi chạy Dev Backend tại: ${backendPath} (bằng ${hasVenv ? "venv python" : "py launcher"})`,
        );

        pyProc = spawn(pythonBin, [backendPath], {
            cwd: path.join(__dirname, "../backend"),
            env: securityHardenedEnv({
                SUBFORGE_DESKTOP: "true",
                // Backend canh tiến trình này còn sống để không tự tắt oan (utils/canh_app.py); cổng muốn giữ sau restart.
                SUBFORGE_PARENT_PID: String(process.pid),
                SUBFORGE_PREFERRED_PORT: congBackendMongMuon ? String(congBackendMongMuon) : "",
                SUBFORGE_DEV: "true",
                SUBFORGE_APP_VERSION: app.getVersion(),
                SUBFORGE_DATA_ROOT: getWorkstationDir(),
                SUBFORGE_ADMIN_URL: RESOLVED_ADMIN_URL,
                SUBFORGE_DESKTOP_SECRET: DESKTOP_SECRET,
                // Dev: tro toi backend/ai_models (chay download_models.py de tao)
                SUBFORGE_MODELS_DIR: path.join(
                    __dirname,
                    "../backend/ai_models",
                ),
                SUBFORGE_GPU_PACK_URL: GPU_PACK_URL || "",
                SUBFORGE_GPU_PACK_SHA256: GPU_PACK_SHA256 || "",
                SUBFORGE_GPU_PACK_VERSION: GPU_PACK_VERSION || "",
                SUBFORGE_DEEPSEEK_API_KEY: DEEPSEEK_API_KEY || "",
                SUBFORGE_DEEPSEEK_BASE_URL:
                    DEEPSEEK_BASE_URL || "https://api.deepseek.com",
                SUBFORGE_DEEPSEEK_MODELS: (DEEPSEEK_MODELS || []).join(","),
                SUBFORGE_DEEPSEEK_DEFAULT_MODEL: DEEPSEEK_DEFAULT_MODEL || "",
                SUBFORGE_GEMINI_API_KEY: GEMINI_API_KEY || "",
                SUBFORGE_GEMINI_BASE_URL: GEMINI_BASE_URL || "",
                SUBFORGE_GEMINI_MODELS: (GEMINI_MODELS || []).join(","),
                SUBFORGE_GEMINI_DEFAULT_MODEL: GEMINI_DEFAULT_MODEL || "",
            }),
        });
    }

    if (pyProc) {
        pyProc.stdout.on("data", (data) => {
            log(`[Backend stdout]: ${data.toString().trim()}`);
        });

        pyProc.stderr.on("data", (data) => {
            log(`[Backend stderr]: ${data.toString().trim()}`);
        });

        pyProc.on("close", (code) => {
            log(`Backend process exited with code ${code}`);
            // Đánh dấu đã tắt để không cố kill lại 1 tiến trình đã chết khi thoát app.
            pyProc = null;
            xuLyBackendThoat(code);
        });
    }
}

// Tắt tiến trình Python Backend
function killPythonSubprocess() {
    tatBackendChuDong = true;
    const pid = pyProc ? pyProc.pid : null;
    if (pyProc) {
        log("Đang tắt tiến trình Python backend...");
        // Gỡ listener trước khi kill: handler 'close'/'data' sẽ gọi log() ở tick
        // sau, nếu lúc đó logStream đã .end() sẽ gây ERR_STREAM_WRITE_AFTER_END.
        try {
            if (pyProc.stdout) pyProc.stdout.removeAllListeners();
            if (pyProc.stderr) pyProc.stderr.removeAllListeners();
            pyProc.removeAllListeners();
        } catch (e) {
            /* ignore */
        }
        try {
            pyProc.kill();
        } catch (e) {
            /* có thể đã tắt */
        }

        // Windows: taskkill ĐỒNG BỘ để tắt sạch cả cây tiến trình TRƯỚC khi app thoát.
        // Dùng exec (bất đồng bộ) trước đây khiến app không thoát hẳn / để lại tiến trình
        // ngầm. Bọc try/catch vì pid có thể đã chết (taskkill trả lỗi).
        if (process.platform === "win32" && pid) {
            try {
                // execFileSync + mảng đối số: không qua shell, pid truyền thẳng (an toàn).
                execFileSync("taskkill", ["/f", "/t", "/pid", String(pid)], {
                    stdio: "ignore",
                    timeout: 5000,
                });
            } catch (e) {
                log(`Taskkill: tiến trình ${pid} có thể đã tắt sẵn.`);
            }
        }
        pyProc = null;
    }

    // LUÔN dọn theo CỔNG (kể cả khi pyProc đã null). RỦI RO: người dùng bấm Tắt
    // KHI ĐANG RENDER/PHÂN TÍCH — uvicorn --reload sinh tiến trình con + worker
    // multiprocessing (demucs/TTS). Nếu tiến trình CHA (pyProc) đã thoát trước thì
    // taskkill /t theo pid ở trên vô tác dụng, để lại CON MỒ CÔI giữ cổng 8000 +
    // VRAM GPU -> lần chạy sau "cổng đã bị chiếm" (đúng ca đã gặp). Quét & kill mọi
    // process còn LISTENING trên dải cổng backend để tắt sạch, không phụ thuộc pid.
    try {
        killStaleBackendPorts();
    } catch (e) {
        log(`[PORT] Dọn cổng backend khi tắt gặp lỗi: ${e.message}`);
    }
}

// Hàm load URL có tự động thử lại khi Server chưa sẵn sàng
function loadURLWithRetry(window, url) {
    window.loadURL(url).catch((err) => {
        log(`Chưa thể kết nối tới ${url}, đang thử lại sau 500ms...`);
        setTimeout(() => {
            if (!window.isDestroyed()) {
                loadURLWithRetry(window, url);
            }
        }, 500);
    });
}

function compareVersions(a, b) {
    const clean = (value) =>
        String(value)
            .split(/[^0-9]+/)
            .filter(Boolean)
            .map((v) => parseInt(v, 10) || 0);
    const aParts = clean(a);
    const bParts = clean(b);
    const len = Math.max(aParts.length, bParts.length);
    for (let i = 0; i < len; i += 1) {
        const aNum = aParts[i] || 0;
        const bNum = bParts[i] || 0;
        if (aNum > bNum) return 1;
        if (aNum < bNum) return -1;
    }
    return 0;
}

let lastNotifiedVersion = null;

// Đăng ký IPC sớm — tránh renderer gọi trước khi app.whenReady() chạy xong
ipcMain.handle("dialog:saveVideo", async (_event, options = {}) => {
    const win = BrowserWindow.getFocusedWindow() || mainWindow;
    const defaultName = options.defaultName || "subforge_export.mp4";
    // Suy đuôi file từ defaultName (vd .mp3 khi tải âm thanh, .mp4 khi video) để
    // filter + đuôi ép ĐÚNG loại — trước đây hardcode "Video MP4" nên tải mp3 vẫn
    // hiện "Save as type: Video MP4".
    const m = /\.([a-z0-9]{2,4})$/i.exec(defaultName.trim());
    const ext = (m ? m[1] : "mp4").toLowerCase();
    const isAudio = ["mp3", "m4a", "wav", "aac", "opus", "ogg", "flac"].includes(ext);
    const filterName = isAudio ? `Âm thanh ${ext.toUpperCase()}` : `Video ${ext.toUpperCase()}`;
    const result = await dialog.showSaveDialog(win ?? undefined, {
        title: options.title || "Lưu tệp xuất",
        defaultPath: defaultName,
        filters: [
            { name: filterName, extensions: [ext] },
            { name: "Tất cả tệp", extensions: ["*"] },
        ],
    });
    if (result.canceled || !result.filePath) {
        return { canceled: true };
    }
    let filePath = result.filePath;
    if (!filePath.toLowerCase().endsWith("." + ext)) {
        filePath += "." + ext;
    }
    return { canceled: false, filePath };
});

// Mở link ra TRÌNH DUYỆT HỆ THỐNG. Phải làm ở main process: preload chạy sandboxed
// nên `shell` là undefined ở đó (đã kiểm chứng: process.sandboxed = true).
// Kiểm tra scheme ĐẶT Ở ĐÂY, không đặt ở preload — renderer có thể bị XSS chi phối,
// còn main thì không; chặn ở main nghĩa là dù renderer bị chiếm cũng không mở được
// scheme lạ (file:, vbscript:, ms-msdt:...) để lừa OS chạy chương trình ngoài.
ipcMain.handle("shell:openExternal", async (_event, url) => {
    if (!url) return false;
    const raw = String(url).trim();
    if (!raw) return false;

    const isWeb = /^https?:\/\//i.test(raw);
    const isMailto = /^mailto:/i.test(raw);

    // QUAN TRỌNG: chuỗi đã mang scheme KHÁC http/https/mailto thì CHẶN THẲNG, tuyệt
    // đối không cố thêm "https://" vào đầu. Thứ tự này là cả vấn đề: nếu thêm prefix
    // trước rồi mới xét protocol thì "file:///C:/Windows/System32/calc.exe" biến thành
    // "https://file:///C:/..." và new URL() báo protocol là "https:" -> LỌT qua cửa
    // kiểm tra, tức lớp chặn scheme thành vô nghĩa. (Đã kiểm chứng bằng test thật.)
    if (!isWeb && !isMailto && /^[a-z][a-z0-9+.-]*:/i.test(raw)) {
        log(`[SECURITY] Chặn openExternal với scheme không an toàn: ${raw.slice(0, 60)}`);
        return false;
    }

    // Không có scheme -> coi là domain do app truyền (vd "sub-forge-pro.vercel.app").
    const target = isWeb || isMailto ? raw : `https://${raw}`;

    let scheme;
    try {
        scheme = new URL(target).protocol.toLowerCase();
    } catch (e) {
        log(`[SECURITY] URL không hợp lệ cho openExternal: ${raw.slice(0, 60)}`);
        return false;
    }
    if (scheme !== "https:" && scheme !== "http:" && scheme !== "mailto:") {
        log(`[SECURITY] Chặn openExternal với scheme không an toàn: ${scheme}`);
        return false;
    }
    try {
        await shell.openExternal(target);
        return true;
    } catch (err) {
        log(`[SECURITY] Lỗi shell.openExternal: ${err?.message || err}`);
        return false;
    }
});

ipcMain.handle("shell:showItemInFolder", async (_event, filePath) => {
    if (!filePath || typeof filePath !== "string") return false;
    shell.showItemInFolder(filePath);
    return true;
});

// Chọn thư mục (cho cài đặt "Vị trí lưu trữ")
ipcMain.handle("dialog:selectDirectory", async (_event, options = {}) => {
    const win = BrowserWindow.getFocusedWindow() || mainWindow;
    const result = await dialog.showOpenDialog(win ?? undefined, {
        title: options.title || "Chọn thư mục lưu dữ liệu SubForge",
        properties: ["openDirectory", "createDirectory"],
    });
    if (result.canceled || !result.filePaths?.length) {
        return { canceled: true };
    }
    return { canceled: false, path: result.filePaths[0] };
});

// Khởi động lại backend (sau khi di chuyển dữ liệu) — kill, xoá port.json cũ,
// spawn lại (đọc storage.json MỚI để chọn exe + env), đợi port.json rồi reload UI.
let backendRestarting = false;
// Cờ chặn app.quit() khi đang thay cửa sổ (đổi cổng backend sau restart):
// trong tích tắc giữa hủy cửa sổ cũ & tạo cửa sổ mới, 'window-all-closed'
// có thể bắn nhầm làm app thoát.
let recreatingWindow = false;
ipcMain.handle("app:restartBackend", () => khoiDongLaiBackend("yêu cầu từ giao diện"));

/** Gửi một sự kiện xuống cửa sổ chính (bỏ qua nếu chưa có / đã huỷ). */
function guiRenderer(kenh, payload) {
    try {
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(kenh, payload);
    } catch (_) {}
}

/** Backend có trả lời không — hỏi /debug/status, chờ tối đa 5 s. */
function kiemTraBackendSong(port) {
    return new Promise((resolve) => {
        let xong = false;
        const tra = (v) => { if (!xong) { xong = true; resolve(v); } };
        try {
            const req = http.get({ host: "127.0.0.1", port, path: "/debug/status", timeout: 5000 }, (res) => {
                res.resume();
                tra(res.statusCode === 200);
            });
            req.on("timeout", () => { req.destroy(); tra(false); });
            req.on("error", () => tra(false));
        } catch (_) {
            tra(false);
        }
    });
}
ipcMain.handle("backend:kiem-tra", async () => {
    const port = readBackendPort();
    return { song: await kiemTraBackendSong(port), port, dang_chay: !!pyProc };
});

/** Backend thoát mà không ai bảo: báo giao diện + tự khởi động lại (có trần), hết trần thì giao diện hiện nút. */
function xuLyBackendThoat(code) {
    if (tatBackendChuDong || backendRestarting) return;
    const now = Date.now();
    lanTuKhoiDongLai = lanTuKhoiDongLai.filter((t) => now - t < TU_KHOI_DONG_LAI_CUA_SO_MS);
    const conLuot = lanTuKhoiDongLai.length < TU_KHOI_DONG_LAI_TOI_DA;
    log(
        `[BACKEND] Thoát ngoài ý muốn (mã ${code}) — ` +
            (conLuot
                ? `tự khởi động lại (lần ${lanTuKhoiDongLai.length + 1}/${TU_KHOI_DONG_LAI_TOI_DA}).`
                : `đã tự khởi động lại ${TU_KHOI_DONG_LAI_TOI_DA} lần trong 10 phút, dừng tự động — chờ người dùng.`),
    );
    guiRenderer("backend:trang-thai", { trang_thai: conLuot ? "dang_khoi_dong_lai" : "bo_cuoc", ma: code });
    if (!conLuot) return;
    lanTuKhoiDongLai.push(now);
    khoiDongLaiBackend(`tự động sau khi backend thoát mã ${code}`).then((r) => {
        if (!r.ok) guiRenderer("backend:trang-thai", { trang_thai: "bo_cuoc", ma: code, loi: r.error });
    });
}

/** Khởi động lại backend: kill (nếu còn), xoá port.json cũ, spawn lại (GIỮ cổng cũ nếu được), đợi port.json. */
async function khoiDongLaiBackend(lyDo) {
    if (backendRestarting) return { ok: false, error: "Đang khởi động lại..." };
    backendRestarting = true;
    try {
        log(`[RESTART] Khởi động lại backend (${lyDo})...`);
        const oldPort = readBackendPort();
        killPythonSubprocess();
        try {
            const portFile = getBackendPortFile();
            if (fs.existsSync(portFile)) fs.unlinkSync(portFile);
        } catch (e) {
            log(`[RESTART] Không xoá được port.json cũ: ${e.message}`);
        }
        congBackendMongMuon = oldPort;
        startPythonSubprocess();
        // Đợi backend ghi port.json mới (tối đa 90s)
        const portFile = getBackendPortFile();
        const deadline = Date.now() + 90_000;
        while (Date.now() < deadline) {
            if (fs.existsSync(portFile)) {
                const port = readBackendPort();
                log(`[RESTART] Backend đã sẵn sàng trên cổng ${port}.`);
                // Cổng ĐỔI sau restart: preload nhận port qua argv lúc TẠO cửa sổ
                // (--backend-port), reload không cập nhật được -> tạo lại cửa sổ.
                if (port !== oldPort && mainWindow && !mainWindow.isDestroyed()) {
                    log(`[RESTART] Cổng đổi ${oldPort} -> ${port}: tạo lại cửa sổ.`);
                    // QUAN TRỌNG: tạo cửa sổ MỚI trước rồi mới hủy cửa sổ cũ.
                    // Nếu hủy trước, số cửa sổ về 0 -> 'window-all-closed' bắn ->
                    // app.quit() khiến app thoát hẳn thay vì mở lại (bug sau migrate).
                    recreatingWindow = true;
                    const oldWin = mainWindow;
                    mainWindow = null;
                    try {
                        createWindow();
                    } finally {
                        try {
                            if (oldWin && !oldWin.isDestroyed()) oldWin.destroy();
                        } catch (_) {}
                        recreatingWindow = false;
                    }
                }
                guiRenderer("backend:trang-thai", { trang_thai: "san_sang", port, port_doi: port !== oldPort });
                return { ok: true, port, portChanged: port !== oldPort };
            }
            if (!pyProc) {
                //  Tiến trình mới chết ngay lúc khởi động (lỗi nạp / thiếu tệp) — không chờ đủ 90 s vô ích.
                log("[RESTART] Backend thoát ngay khi khởi động.");
                return { ok: false, error: "Backend thoát ngay khi khởi động — xem subforge_desktop.log." };
            }
            await new Promise((r) => setTimeout(r, 300));
        }
        return { ok: false, error: "Backend không khởi động lại được trong 90s." };
    } finally {
        backendRestarting = false;
    }
}

// LẤY BÀI BÁO BẰNG CỬA SỔ TRÌNH DUYỆT CỦA APP (chế độ Sáng tạo) — xem electron/article-window.js.
// Việc kiểm địa chỉ nằm ở module (main process), renderer chỉ chuyển tiếp.
const articleWindow = require("./article-window");
ipcMain.handle("sangtao:baiTuDong", (_event, url) => articleWindow.layTuDong(url));
ipcMain.handle("sangtao:baiMo", (_event, url, hien) => articleWindow.mo(url, { hien: !!hien }));
ipcMain.handle("sangtao:baiLay", (_event, id, opts) =>
    articleWindow.lay(String(id || ""), { cho: !!(opts && opts.cho), dongSau: !!(opts && opts.dongSau) }),
);
ipcMain.handle("sangtao:baiDong", (_event, id) => articleWindow.dong(String(id || "")));

// LẤY COOKIE DOUYIN TỰ ĐỘNG: mở cửa sổ Douyin thật trong app, đợi trang set đủ
// cookie quan trọng (ttwid là HttpOnly — cách copy document.cookie thủ công
// KHÔNG lấy được) rồi tự đóng và trả về nội dung cookies.txt (Netscape) cho
// renderer lưu vào backend. Người dùng chỉ việc đợi trang hiện video (làm
// captcha nếu Douyin yêu cầu).
ipcMain.handle("douyin:harvestCookies", async (event) => {
    const partition = "persist:douyin-cookie";
    const ses = session.fromPartition(partition);
    // Dọn cookie cũ của lần lấy trước để chắc chắn thu về bộ cookie TƯƠI.
    try {
        await ses.clearStorageData({ storages: ["cookies"] });
    } catch {
        /* ignore */
    }

    // Cửa sổ NHỎ neo góc phải-dưới màn hình — không che app chính, người dùng
    // vẫn nhìn được tiến độ quét/tải bên app trong lúc chờ. (Douyin chỉ cần set
    // cookie, layout hẹp không ảnh hưởng.)
    const { screen } = require("electron");
    const wa = screen.getPrimaryDisplay().workArea;
    const winW = 280;
    const winH = 300;
    const win = new BrowserWindow({
        width: winW,
        height: winH,
        x: wa.x + wa.width - winW - 16,
        y: wa.y + wa.height - winH - 16,
        title: "Lấy cookie Douyin — đợi trang hiện video, cửa sổ sẽ tự đóng",
        autoHideMenuBar: true,
        alwaysOnTop: false,
        webPreferences: {
            partition,
            nodeIntegration: false,
            contextIsolation: true,
            sandbox: true,
        },
    });
    // UA mặc định của Electron chứa "Electron/x.y" -> Douyin dễ chặn; giả UA Chrome.
    win.webContents.setUserAgent(
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    );

    // CHẶN mở app ngoài & popup: trang Douyin cố mở "bitbrowser://" / app desktop
    // của nó -> Electron đẩy sang OS làm bật hộp thoại "Get an app to open this
    // link". Chặn mọi điều hướng KHÔNG phải http(s) và mọi cửa sổ bật ra.
    const isWeb = (u) => /^https?:\/\//i.test(u || "");
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    const blockNonWeb = (e, url) => {
        if (!isWeb(url)) e.preventDefault();
    };
    win.webContents.on("will-navigate", blockNonWeb);
    win.webContents.on("will-redirect", blockNonWeb);

    win.loadURL("https://www.douyin.com/");

    // Trang tải xong: CUỘN NHẸ vài lần để JS của Douyin sinh nốt cookie động
    // (msToken thường chỉ xuất hiện sau tương tác) — người dùng khỏi phải làm gì.
    let loadedAt = 0;
    win.webContents.once("did-finish-load", () => {
        loadedAt = Date.now();
        let hops = 0;
        const nudge = setInterval(() => {
            hops += 1;
            if (hops > 4 || win.isDestroyed()) {
                clearInterval(nudge);
                return;
            }
            win.webContents
                .executeJavaScript(`window.scrollBy(0, ${hops * 300});`, true)
                .catch(() => {});
        }, 1200);
    });

    // Tự đóng theo 3 tầng (tránh vừa đóng-quá-sớm vừa treo-mãi):
    //  1. ĐỦ BỘ (ttwid + msToken + >=10 cookie, ổn định 2 nhịp) -> đóng ngay.
    //  2. Trang đã tải xong >10s và có ttwid + >=8 cookie -> chấp nhận (một số
    //     phiên Douyin không set msToken — đợi mãi chỉ treo cửa sổ).
    //  3. Quá 60s kể từ khi mở mà có ttwid -> chốt với những gì thu được.
    const openedAt = Date.now();
    const dyCookies = async () =>
        (await ses.cookies.get({})).filter((c) =>
            (c.domain || "").includes("douyin.com"),
        );
    let stablePolls = 0;
    const poll = setInterval(async () => {
        try {
            const dy = await dyCookies();
            const names = new Set(dy.map((c) => c.name));
            // Bắn tiến độ về app chính để người dùng thấy đang thu được gì.
            try {
                event.sender.send("douyin:harvestProgress", {
                    count: dy.length,
                    hasTtwid: names.has("ttwid"),
                    hasMsToken: names.has("msToken"),
                    elapsedSec: Math.round((Date.now() - openedAt) / 1000),
                });
            } catch {
                /* ignore */
            }
            if (!names.has("ttwid")) {
                stablePolls = 0;
                return;
            }
            const full = dy.length >= 10 && names.has("msToken");
            const goodEnough =
                loadedAt > 0 &&
                Date.now() - loadedAt > 10_000 &&
                dy.length >= 8;
            const timeout = Date.now() - openedAt > 60_000;
            if (full) {
                stablePolls += 1;
            } else {
                stablePolls = 0;
            }
            if ((full && stablePolls >= 2) || goodEnough || timeout) {
                clearInterval(poll);
                if (!win.isDestroyed()) win.close();
            }
        } catch {
            /* ignore */
        }
    }, 1500);

    await new Promise((resolve) => win.once("closed", resolve));
    clearInterval(poll);

    const cookies = (await ses.cookies.get({})).filter((c) =>
        (c.domain || "").includes("douyin.com"),
    );
    if (!cookies.length) {
        return {
            ok: false,
            message:
                "Chưa thu được cookie nào — hãy đợi trang Douyin hiện video rồi thử lại.",
        };
    }
    // Người dùng tự đóng cửa sổ quá sớm -> bộ cookie quá mỏng thường vô dụng;
    // báo rõ thay vì lưu bộ hỏng đè lên cookie đang có.
    const names = new Set(cookies.map((c) => c.name));
    if (!names.has("ttwid") || cookies.length < 6) {
        return {
            ok: false,
            message: `Mới thu được ${cookies.length} cookie — hãy mở lại và ĐỢI trang Douyin hiện video hẳn (cửa sổ sẽ tự đóng khi đủ).`,
        };
    }
    const lines = ["# Netscape HTTP Cookie File"];
    for (const c of cookies) {
        lines.push(
            [
                c.domain,
                c.domain.startsWith(".") ? "TRUE" : "FALSE",
                c.path || "/",
                c.secure ? "TRUE" : "FALSE",
                Math.floor(c.expirationDate || Date.now() / 1000 + 31536000),
                c.name,
                c.value,
            ].join("\t"),
        );
    }
    return { ok: true, count: cookies.length, cookieText: lines.join("\n") };
});

async function checkForLatestVersion() {
    if (LOCAL_MODE) return;
    if (!mainWindow || mainWindow.isDestroyed()) return;

    const portalUrl = RESOLVED_ADMIN_URL;
    const endpoint = `${portalUrl.replace(/\/$/, "")}/api/public/latest`;
    log(`Kiểm tra phiên bản mới từ: ${endpoint}`);

    try {
        const response = await fetch(endpoint, { cache: "no-store" });
        if (!response.ok) {
            log(`Không thể truy vấn bản mới nhất. HTTP ${response.status}`);
            return;
        }

        const payload = await response.json();
        const latest = payload?.latest || payload;
        if (!latest?.version) {
            log("Không tìm thấy thông tin phiên bản mới nhất.");
            return;
        }

        const currentVersion = app.getVersion();
        log(
            `Phiên bản hiện tại: ${currentVersion}, phiên bản mới nhất: ${latest.version}`,
        );

        if (compareVersions(currentVersion, latest.version) < 0) {
            if (latest.version === lastNotifiedVersion) {
                log(
                    `Đã gửi thông báo phiên bản ${latest.version} trước đó, bỏ qua.`,
                );
                return;
            }

            lastNotifiedVersion = latest.version;
            if (!mainWindow.isDestroyed()) {
                mainWindow.webContents.send("desktop-update-available", latest);
                log(
                    `Đã gửi sự kiện cập nhật phiên bản ${latest.version} tới renderer.`,
                );
            }
        }
    } catch (err) {
        log(`Lỗi khi kiểm tra phiên bản mới: ${err?.message || err}`);
    }
}

//  Chiều cao dải tiêu đề tuỳ biến (px) — PHẢI bằng ``--thanh-tieu-de-cao`` trong frontend/src/index.css.
const THANH_TIEU_DE_CAO = 36;   // = --thanh-tieu-de-cao (index.css); renderer còn tự đo lại theo vùng 3 nút thật
//  Màu nền / màu 3 nút cửa sổ theo chủ đề — khớp token ``--background`` / ``--foreground`` của index.css (sáng: trắng, tối: ~#252525).
const MAU_THANH_TIEU_DE = {
    light: { color: "#ffffff", symbolColor: "#262626" },
    dark: { color: "#0a0a0a", symbolColor: "#e5e5e5" },
};
//  Chủ đề đã chọn được ghi ra tệp để lần mở sau MÀN KHỞI ĐỘNG (chưa có renderer) vẽ đúng sáng / tối ngay.
function tepChuDe() {
    return path.join(app.getPath("userData"), "theme.json");
}
//  TRẠNG THÁI CỬA SỔ (vị trí / cỡ / phóng to) — ghi lúc đóng, khôi phục lúc hiện cửa sổ thật (07-10-2026: màn khởi động
//  đổi thành cửa sổ nhỏ nên không còn "chép cỡ từ màn khởi động"; nhớ lần trước để người dùng không phải phóng to lại mỗi lần).
function tepTrangThaiCuaSo() { return path.join(app.getPath("userData"), "cua_so.json"); }
function docTrangThaiCuaSo() {
    try {
        const d = JSON.parse(fs.readFileSync(tepTrangThaiCuaSo(), "utf8"));
        if (!d || typeof d !== "object") return null;
        const b = d.bounds;
        const ok = b && [b.x, b.y, b.width, b.height].every((n) => Number.isFinite(n));
        const zoom = Number.isFinite(d.zoom) && d.zoom >= 0.5 && d.zoom <= 2 ? d.zoom : null;
        return { bounds: ok ? b : null, maximized: !!d.maximized, zoom };
    } catch (_) { return null; }
}
function luuTrangThaiCuaSo(w) {
    if (!w || w.isDestroyed()) return;
    const maximized = w.isMaximized();
    //  Đang phóng to thì getNormalBounds = cỡ trước khi phóng to (lần sau bỏ phóng to vẫn ra cỡ cũ).
    const bounds = maximized ? w.getNormalBounds() : w.getBounds();
    let zoom = null;
    try { zoom = w.webContents.getZoomFactor(); } catch (_) {}
    try { fs.writeFileSync(tepTrangThaiCuaSo(), JSON.stringify({ bounds, maximized, zoom })); } catch (_) {}
}

//  ZOOM MẶC ĐỊNH THEO CỠ MÀN HÌNH (người dùng 09-10-2026): máy 14 inch (1366×768, hoặc 1920×1080 đặt 125–150 % →
//  chỉ còn 1280×720 / 1536×864 điểm logic) thì giao diện choán hết màn, khó nhìn → mặc định 75 %. Màn lớn giữ 100 %.
//  Người dùng chỉnh Ctrl + / − thì NHỚ (cua_so.json) và không áp mặc định nữa; Ctrl + 0 về đúng mặc định của màn này.
const ZOOM_MAY_NHO = 0.75;
const CAO_MAN_NHO = 870;            // điểm logic; 1366×768 → 768, 1920×1080 @125 % → 864, @100 % → 1040 (giữ 100 %)
function zoomMacDinhTheoMan() {
    try {
        const { screen } = require("electron");
        const wa = screen.getPrimaryDisplay().workAreaSize;
        return wa && wa.height <= CAO_MAN_NHO ? ZOOM_MAY_NHO : 1.0;
    } catch (_) {
        return 1.0;
    }
}
function apZoomBanDau(w) {
    if (!w || w.isDestroyed()) return;
    const tt = docTrangThaiCuaSo();
    const z = tt && tt.zoom ? tt.zoom : zoomMacDinhTheoMan();
    try {
        w.webContents.setZoomFactor(z);
        if (z !== 1.0) log(`[ZOOM] Áp zoom ${Math.round(z * 100)}% (${tt && tt.zoom ? "đã nhớ" : "mặc định theo cỡ màn hình"}).`);
    } catch (_) {}
}
function doiZoom(w, z) {
    if (!w || w.isDestroyed()) return;
    const moi = Math.min(2, Math.max(0.5, Math.round(z * 20) / 20));   // bước 5 %, kẹp 50–200 %
    try { w.webContents.setZoomFactor(moi); } catch (_) {}
    //  Ghi ngay (không đợi lúc đóng): app tắt bất thường thì lần sau vẫn đúng mức người dùng vừa chọn.
    try {
        const cu = (() => { try { return JSON.parse(fs.readFileSync(tepTrangThaiCuaSo(), "utf8")) || {}; } catch (_) { return {}; } })();
        fs.writeFileSync(tepTrangThaiCuaSo(), JSON.stringify({ ...cu, zoom: moi }));
    } catch (_) {}
}

function docChuDe() {
    try {
        const t = JSON.parse(fs.readFileSync(tepChuDe(), "utf8")).theme;
        return t === "dark" ? "dark" : "light";
    } catch (_) {
        return nativeTheme && nativeTheme.shouldUseDarkColors ? "dark" : "light";
    }
}
//  Renderer hỏi "đang toàn màn hình không?" lúc mới nạp (F11 bấm trước khi React mount, hoặc F5 giữa lúc toàn màn hình).
ipcMain.handle("cua-so:toan-man-hinh?", () => {
    try {
        return !!(mainWindow && !mainWindow.isDestroyed() && mainWindow.isFullScreen());
    } catch (_) {
        return false;
    }
});

ipcMain.handle("titlebar:theme", (_evt, theme) => {
    const m = MAU_THANH_TIEU_DE[theme === "dark" ? "dark" : "light"];
    try {
        fs.writeFileSync(tepChuDe(), JSON.stringify({ theme: theme === "dark" ? "dark" : "light" }));
    } catch (_) {}
    try {
        if (mainWindow && !mainWindow.isDestroyed() && typeof mainWindow.setTitleBarOverlay === "function") {
            mainWindow.setTitleBarOverlay({ ...m, height: THANH_TIEU_DE_CAO });
            mainWindow.setBackgroundColor(m.color);
        }
    } catch (e) {
        log(`[TITLEBAR] Không đổi được màu thanh tiêu đề: ${e.message}`);
    }
    return true;
});

//  MÀN KHỞI ĐỘNG (07-10-2026, người dùng: "bấm icon là phải thấy app lên ngay"): trước đây cửa sổ chỉ được tạo SAU khi
//  backend ghi port.json (10–30 s lần đầu) nên bấm icon xong là… không có gì. Nay mở ngay một cửa sổ cùng cỡ với trang
//  khoi_dong.html (logo + thanh chạy), rồi khi giao diện thật đã vẽ (renderer báo IPC "app:ui-ready") mới thay. Đóng màn này khi backend
//  chưa lên = huỷ mở app (``window-all-closed`` → quit, backend con bị dọn theo).
let cuaSoKhoiDong = null;
//  Hàm hiện cửa sổ thật của lần tạo gần nhất — renderer báo "đã vẽ giao diện" (IPC app:ui-ready, App.tsx bắn SAU khi kiểm tra
//  bản quyền xong) thì gọi.
let hienCuaSoThat = null;
ipcMain.on("app:ui-ready", () => {
    if (typeof hienCuaSoThat === "function") hienCuaSoThat();
});

//  TRẠNG THÁI trên màn khởi động (người dùng chốt 07-10-2026: MỘT màn nhận trạng thái cả backend lẫn giao diện):
//    bước 1 "Khởi động dịch vụ xử lý" — từ lúc spawn backend tới khi có port.json;
//    bước 2 "Tải giao diện"           — cửa sổ thật nạp trang, kiểm tra bản quyền; React vẽ xong → đóng màn khởi động.
//  Đẩy bằng executeJavaScript (màn khởi động sandbox, không preload); gọi trước khi trang nạp xong thì nhớ lại và đẩy khi nạp xong.
let trangThaiKhoiDong = { buoc: 1, mo_ta: "Đang khởi động dịch vụ xử lý…" };
function baoKhoiDong(buoc, moTa) {
    trangThaiKhoiDong = { buoc, mo_ta: moTa };
    const w = cuaSoKhoiDong;
    if (!w || w.isDestroyed() || w.webContents.isLoading()) return;
    w.webContents.executeJavaScript(`window.datTrangThai && window.datTrangThai(${JSON.stringify(trangThaiKhoiDong)})`, true)
        .catch(() => {});
}

//  Cỡ cửa sổ khởi động — khớp bố cục khoi_dong.html.
const KHUNG_KHOI_DONG = { width: 460, height: 384 };
//  Cửa sổ thật đã hiện chưa — người dùng bấm × trên màn khởi động TRƯỚC lúc đó = thoát app (cửa sổ thật còn ẩn nên
//  ``window-all-closed`` không nổ; không có cờ này app chạy ngầm mãi).
let cuaSoThatDaHien = false;

//  NÚT × trên màn khởi động = THOÁT APP (người dùng báo 08-10-2026 "bấm không có tác dụng"): trước đây màn gọi
//  ``window.close()`` → ``closed`` → ``app.quit()``, nhưng cửa sổ thật đang ẨN có bộ chặn 'close' (hỏi backend có việc
//  nặng không rồi mới đóng) nên quit bị giữ lại, app đứng nguyên. Nay: huỷ thẳng cửa sổ thật chưa hiện (``destroy`` bỏ
//  qua bộ chặn), đóng màn khởi động rồi quit — backend con bị dọn ở ``will-quit`` như thường.
ipcMain.on("khoi-dong:thoat", () => {
    log("[KHỞI ĐỘNG] Người dùng bấm × trên màn khởi động — thoát app.");
    try {
        if (mainWindow && !mainWindow.isDestroyed() && !cuaSoThatDaHien) mainWindow.destroy();
    } catch (_) {}
    dongCuaSoKhoiDong();
    app.quit();
});

function taoCuaSoKhoiDong() {
    try {
        const chuDe = docChuDe();
        cuaSoKhoiDong = new BrowserWindow({
            ...KHUNG_KHOI_DONG,
            title: "SubForge Pro",
            icon: path.join(__dirname, "icon.png"),
            frame: false,
            //  ĐỤC, không transparent: cửa sổ trong suốt trên Windows có lúc không đưa cú bấm tới nút no-drag (08-10-2026,
            //  nút × không ăn); Windows 11 vẫn tự bo góc cửa sổ không viền. Màu nền theo chủ đề để không nháy trắng.
            transparent: false,
            hasShadow: true,
            resizable: false,
            maximizable: false,
            fullscreenable: false,
            center: true,
            backgroundColor: chuDe === "dark" ? "#121212" : "#ffffff",
            autoHideMenuBar: true,
            webPreferences: {
                preload: path.join(__dirname, "khoi_dong_preload.js"),
                contextIsolation: true,
                nodeIntegration: false,
                sandbox: true,
            },
        });
        cuaSoKhoiDong.setMenuBarVisibility(false);
        cuaSoKhoiDong
            .loadFile(path.join(__dirname, "khoi_dong.html"), { query: { theme: chuDe, v: app.getVersion() } })
            .catch((e) => log(`[KHỞI ĐỘNG] ${e.message}`));
        cuaSoKhoiDong.webContents.once("did-finish-load", () => baoKhoiDong(trangThaiKhoiDong.buoc, trangThaiKhoiDong.mo_ta));
        cuaSoKhoiDong.on("closed", () => {
            cuaSoKhoiDong = null;
            if (!cuaSoThatDaHien) {
                log("[KHỞI ĐỘNG] Người dùng đóng màn khởi động trước khi app lên — thoát.");
                app.quit();
            }
        });
    } catch (e) {
        log(`[KHỞI ĐỘNG] Không mở được màn khởi động: ${e.message}`);
        cuaSoKhoiDong = null;
    }
}

function dongCuaSoKhoiDong() {
    const w = cuaSoKhoiDong;
    cuaSoKhoiDong = null;
    try {
        if (w && !w.isDestroyed()) w.destroy();
    } catch (_) {}
}

function createWindow() {
    const port = readBackendPort();
    log(`[PORT] Khởi tạo cửa sổ ứng dụng kết nối tới backend port: ${port}`);

    mainWindow = new BrowserWindow({
        width: 1366,
        height: 768,
        minWidth: 1024,
        minHeight: 600,
        title: "SubForge Pro Workstation",
        //  Icon cửa sổ / thanh tác vụ. Bản đóng gói lấy icon từ .exe (electron-builder
        //  nhúng build/icon.ico), còn chạy từ nguồn thì đọc tệp này — không có nó là
        //  thấy logo Electron mặc định.
        icon: path.join(__dirname, "icon.png"),
        //  THANH TIÊU ĐỀ TUỲ BIẾN (07-10-2026, người dùng chê thanh "SubForge" mặc định của Windows lệch tông app):
        //  ẩn thanh hệ thống, giữ 3 nút thu nhỏ / phóng to / đóng do Windows vẽ (overlay) và tô màu theo chủ đề sáng/tối
        //  (``setTitleBarOverlay`` qua IPC "titlebar:theme"); renderer tự vẽ dải tiêu đề (36 px, tự đo lại theo vùng 3 nút) có vùng kéo (``.thanh-tieu-de``).
        titleBarStyle: "hidden",
        //  Màu nền + 3 nút theo CHỦ ĐỀ ĐÃ CHỌN ngay từ đầu (theme.json) — người dùng chủ đề tối từng thấy khung trắng.
        titleBarOverlay: { ...MAU_THANH_TIEU_DE[docChuDe()], height: THANH_TIEU_DE_CAO },
        backgroundColor: MAU_THANH_TIEU_DE[docChuDe()].color,
        //  Ẩn tới khi trang nạp xong; xem khối "HIỆN CỬA SỔ THẬT" bên dưới.
        show: false,
        webPreferences: {
            preload: path.join(__dirname, "preload.js"),
            contextIsolation: true,
            nodeIntegration: false,
            // Không bóp throttle timer khi cửa sổ bị minimize/ẩn -> giữ nhịp
            // heartbeat 5s tới backend ngay cả khi render dài chạy nền.
            backgroundThrottling: false,
            // app.getVersion() chi chay dung o MAIN process; truyen sang preload
            // qua argv vi `app` khong ton tai trong preload (renderer) -> tranh
            // appVersion bi roi ve fallback "1.0.0".
            additionalArguments: [
                `--backend-port=${port}`,
                `--admin-url=${LOCAL_MODE ? `http://127.0.0.1:${port}` : RESOLVED_ADMIN_URL}`,
                `--app-version=${app.getVersion()}`,
            ],
        },
    });

    //  HIỆN CỬA SỔ THẬT — ĐÚNG MỘT LẦN, khi React đã vẽ xong màn thật (IPC "app:ui-ready" từ App.tsx, SAU kiểm tra bản
    //  quyền). Trước đó cửa sổ ở ẩn hoàn toàn (``show: false``; Electron vẫn vẽ cửa sổ ẩn — ``paintWhenInitiallyHidden`` mặc
    //  định bật) nên nền trang + màn chờ "Tải giao diện" của React không ai thấy; màn khởi động là màn duy nhất tới lúc đó.
    //  BẪY ĐÃ GẶP (07-10-2026, "nháy 2 cửa sổ"): bản trước ``showInactive()`` cửa sổ thật NẰM SAU màn khởi động rồi
    //  ``maximize()`` cho khớp — trên Windows lệnh phóng to luôn đưa cửa sổ lên TRƯỚC, nên màn chờ React lóe lên một khung rồi
    //  màn khởi động mới được đẩy lại lên trên. Nay: đặt sẵn KÍCH THƯỚC theo lần trước (cua_so.json) khi còn ẩn → ``show()`` →
    //  ``maximize()`` nếu lần trước phóng to → đóng màn khởi động. Nền trang tối từ khung đầu nhờ
    //  script trong frontend/index.html + ``backgroundColor`` theo chủ đề.
    //  Lưới an toàn 75 s (60 s vòng chờ backend của App + dư): không có tín hiệu (trang lỗi) thì vẫn hiện.
    //  KHÔNG dùng ``ready-to-show``: nó bắn ngay khi trang trắng đầu tiên được vẽ (React chưa mount).
    {
        let daHien = false;
        const cuaSoNay = mainWindow;
        const hien = () => {
            if (daHien || !cuaSoNay || cuaSoNay.isDestroyed() || mainWindow !== cuaSoNay) return;
            daHien = true;
            cuaSoThatDaHien = true;
            //  Vị trí / cỡ / phóng to của LẦN TRƯỚC (cua_so.json) — màn khởi động nay nhỏ nên không còn gì để chép từ nó.
            const tt = docTrangThaiCuaSo();
            const phongTo = !!(tt && tt.maximized);
            try {
                if (tt && tt.bounds) {
                    const { screen } = require("electron");
                    const b = tt.bounds;
                    const wa = screen.getDisplayMatching(b).workArea;
                    //  Màn hình cũ không còn (tháo màn phụ) thì bỏ, dùng cỡ mặc định ở giữa.
                    if (b.x + b.width > wa.x + 40 && b.x < wa.x + wa.width - 40 && b.y >= wa.y - 20 && b.y < wa.y + wa.height - 40) {
                        cuaSoNay.setBounds({ x: b.x, y: b.y, width: Math.max(1024, b.width), height: Math.max(600, b.height) });
                    }
                }
            } catch (_) {}
            cuaSoNay.show();
            if (phongTo) { try { cuaSoNay.maximize(); } catch (_) {} }
            cuaSoNay.focus();
            dongCuaSoKhoiDong();
        };
        cuaSoNay.webContents.once("did-finish-load", () => {
            log("[UI] did-finish-load");
            baoKhoiDong(2, "Đang tải giao diện…");
            if (LOCAL_MODE) hien();
        });
        cuaSoNay.webContents.on("dom-ready", () => log("[UI] dom-ready"));
        cuaSoNay.webContents.on("did-fail-load", (_event, code, description, url) => {
            log(`[UI] Load failed ${code}: ${description}: ${url}`);
        });
        cuaSoNay.webContents.on("console-message", (_event, level, message, line, source) => {
            log(`[UI:${level}] ${message} (${source}:${line})`);
        });
        cuaSoNay.webContents.on("render-process-gone", (_event, details) => {
            log(`[UI] Renderer exited: ${JSON.stringify(details)}`);
        });
        hienCuaSoThat = hien;
        setTimeout(hien, 75000);
    }

    // Bỏ menu mặc định của Electron để app giống chuyên nghiệp hơn.
    // setMenuBarVisibility chỉ ẨN menu — accelerator của nó (Ctrl+Shift+I =
    // Toggle DevTools, Ctrl+Shift+C, Ctrl+Shift+J) VẪN hoạt động. Ở bản đóng gói
    // phải xoá hẳn menu, nếu không thì việc chặn F12 bên dưới là vô nghĩa.
    mainWindow.setMenuBarVisibility(false);
    if (app.isPackaged) {
        Menu.setApplicationMenu(null);
        // Chốt hạ cuối: bất kể mở DevTools bằng đường nào (menu, accelerator,
        // lệnh nội bộ), đóng lại ngay. Rẻ hơn việc cố liệt kê đủ mọi lối vào.
        mainWindow.webContents.on("devtools-opened", () => {
            mainWindow.webContents.closeDevTools();
        });
    }

    // BẢO MẬT: chặn điều hướng ra origin lạ và mở cửa sổ Electron mới.
    // Link ngoài (http/https) được đẩy sang trình duyệt hệ thống thay vì load
    // trong khung ứng dụng.
    const isInternalNavigation = (target) => {
        try {
            const u = new URL(target);
            if (u.protocol === "file:") return true;
            return u.hostname === "localhost" || u.hostname === "127.0.0.1";
        } catch (e) {
            return false;
        }
    };
    mainWindow.webContents.on("will-navigate", (event, target) => {
        if (!isInternalNavigation(target)) {
            event.preventDefault();
            if (/^https?:\/\//i.test(target)) shell.openExternal(target);
        }
    });
    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        if (/^https?:\/\//i.test(url)) shell.openExternal(url);
        return { action: "deny" };
    });

    //  TOÀN MÀN HÌNH: báo renderer để ẩn dải tiêu đề tuỳ biến (ThanhTieuDe) — ở toàn màn hình Windows không vẽ 3 nút
    //  overlay nữa, dải 36 px còn lại chỉ là một vạch trống chiếm chỗ (người dùng 09-10-2026: "F11 không ẩn thanh trên").
    {
        const bao = (bat) => {
            try {
                if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send("cua-so:toan-man-hinh", !!bat);
            } catch (_) {}
        };
        mainWindow.on("enter-full-screen", () => bao(true));
        mainWindow.on("leave-full-screen", () => bao(false));
    }

    // Hỗ trợ phím tắt điều hướng trong ứng dụng Desktop
    mainWindow.webContents.on("before-input-event", (event, input) => {
        //  F11: bật / tắt toàn màn hình. Menu mặc định đã bị gỡ (``Menu.setApplicationMenu(null)``) nên accelerator F11
        //  của Electron không còn — phải tự bắt ở đây.
        if (input.key === "F11" && input.type === "keyDown") {
            try { mainWindow.setFullScreen(!mainWindow.isFullScreen()); } catch (_) {}
            event.preventDefault();
            return;
        }
        // F5 hoặc Ctrl+R để reload trang khi gặp lỗi
        if (
            (input.control && input.key.toLowerCase() === "r") ||
            input.key === "F5"
        ) {
            mainWindow.reload();
            event.preventDefault();
        }
        // F12 để bật/tắt DevTools gỡ lỗi — chỉ cho phép ở môi trường dev,
        // tắt trong bản đóng gói để người dùng cuối không mở được DevTools.
        if (input.key === "F12" && !app.isPackaged) {
            mainWindow.webContents.toggleDevTools();
            event.preventDefault();
        }
        // Bản đóng gói: chặn luôn các tổ hợp mở DevTools của Chromium
        // (Ctrl+Shift+I / J / C) — F12 không phải lối vào duy nhất.
        if (
            app.isPackaged &&
            input.control &&
            input.shift &&
            ["i", "j", "c"].includes((input.key || "").toLowerCase())
        ) {
            event.preventDefault();
        }
        // Alt + Mũi tên trái để Quay lại trang cũ (Go Back)
        if (input.alt && input.key === "ArrowLeft") {
            if (mainWindow.webContents.canGoBack()) {
                mainWindow.webContents.goBack();
            }
            event.preventDefault();
        }
        //  Phóng to / thu nhỏ theo HỆ SỐ (bước 10 %) và NHỚ lại — xem ``doiZoom``. Chỉ bắt keyDown: mỗi lần bấm
        //  Chromium gửi cả keyDown lẫn keyUp, bản cũ không lọc nên một nhát Ctrl + bị đổi hai nấc.
        if (input.type === "keyDown") {
            // Phóng to: Ctrl + '+' hoặc Ctrl + '='
            if (input.control && (input.key === "=" || input.key === "+")) {
                doiZoom(mainWindow, mainWindow.webContents.getZoomFactor() + 0.1);
                event.preventDefault();
            }
            // Thu nhỏ: Ctrl + '-'
            if (input.control && input.key === "-") {
                doiZoom(mainWindow, mainWindow.webContents.getZoomFactor() - 0.1);
                event.preventDefault();
            }
            // Ctrl + '0': về MẶC ĐỊNH CỦA MÀN NÀY (máy nhỏ = 75 %, không phải 100 %).
            if (input.control && input.key === "0") {
                doiZoom(mainWindow, zoomMacDinhTheoMan());
                event.preventDefault();
            }
        }
    });

    if (!app.isPackaged && !LOCAL_MODE) {
        // Dev Mode: Đọc từ Vite Server
        const devUrl = "http://localhost:5173";
        log(`Đang tải giao diện Development: ${devUrl}`);
        loadURLWithRetry(mainWindow, devUrl);
        // Tự động mở DevTools trong Dev
        mainWindow.webContents.openDevTools();
    } else {
        // Production Mode: Load file tĩnh từ thư mục build
        const prodPath = path.join(__dirname, "../frontend/dist/index.html");
        log(`Đang tải giao diện Production: ${prodPath}`);
        mainWindow.loadFile(prodPath).catch((err) => {
            log(`Lỗi khi tải file giao diện: ${err.message}`);
        });
    }

    //  HỎI LẠI KHI ĐÓNG APP GIỮA LÚC MÁY ĐANG LÀM VIỆC NẶNG.
    //
    //  Vì sao cần: lấy phụ đề / tách giọng / sinh giọng / dựng video chạy ở backend
    //  và có thể kéo dài hàng giờ (lô 20 video). Đóng app là tiến trình backend bị
    //  tắt theo -> việc đang chạy mất trắng, tệp dở nằm lại, và lần mở sau phải
    //  chạy lại từ đầu. Trước đây đóng là đóng, không một lời cảnh báo.
    //
    //  Hỏi backend chứ không hỏi giao diện: giao diện có thể đang ở trang khác,
    //  còn backend thì luôn biết chính xác đang chạy gì (hàng đợi việc nặng).
    let dangDongThat = false;
    mainWindow.on("close", (e) => {
        if (dangDongThat) return;      // lượt đóng thứ hai (người dùng đã xác nhận)
        //  Cửa sổ CHƯA TỪNG HIỆN (đang ở màn khởi động, app.quit() từ nút ×): chưa có việc gì để giữ — cho đóng thẳng,
        //  không hỏi backend (nó có thể chưa lên; hỏi là treo lệnh thoát).
        if (!cuaSoThatDaHien) return;
        e.preventDefault();

        const ketThuc = () => {
            dangDongThat = true;
            if (mainWindow && !mainWindow.isDestroyed()) mainWindow.close();
        };

        hoiViecDangChay()
            .then((viec) => {
                if (!viec) return ketThuc();
                const chon = dialog.showMessageBoxSync(mainWindow, {
                    type: "warning",
                    buttons: ["Để chạy tiếp", "Vẫn đóng"],
                    //  Nút mặc định VÀ nút khi bấm Esc đều là "Để chạy tiếp": lỡ tay
                    //  Enter/Esc thì không mất việc đang chạy hàng giờ.
                    defaultId: 0,
                    cancelId: 0,
                    noLink: true,
                    title: "Máy đang chạy việc nặng",
                    message: viec.tieu_de,
                    detail: viec.chi_tiet,
                });
                if (chon === 1) ketThuc();
            })
            .catch((err) => {
                //  Không hỏi được backend (đã tắt / lỗi mạng) thì ĐỪNG giữ cửa sổ —
                //  người dùng bấm đóng mà app không đóng là lỗi tệ hơn hẳn.
                log(`[ĐÓNG APP] Không kiểm được việc đang chạy: ${err.message}`);
                ketThuc();
            });
    });

    //  Chỉ nhớ vị trí của cửa sổ ĐÃ HIỆN — cửa sổ bị huỷ lúc còn ẩn mang cỡ mặc định, lưu là mất trạng thái lần trước.
    mainWindow.on("close", () => { try { if (cuaSoThatDaHien) luuTrangThaiCuaSo(mainWindow); } catch (_) {} });
    mainWindow.on("closed", () => {
        mainWindow = null;
    });

    mainWindow.webContents.on("did-finish-load", () => {
        //  Zoom đã nhớ / mặc định theo cỡ màn hình — áp mỗi lần nạp trang (F5 cũng giữ đúng mức).
        apZoomBanDau(mainWindow);
        if (!LOCAL_MODE) checkForLatestVersion();
        if (!LOCAL_MODE && !versionCheckInterval) {
            versionCheckInterval = setInterval(
                checkForLatestVersion,
                1000 * 60 * 60,
            ); // kiểm tra mỗi giờ
        }
    });
}

// Hàm chờ file port.json sẵn sàng trước khi tạo cửa sổ.
// LƯU Ý: port.json CŨ đã được xoá ở app.whenReady TRƯỚC khi spawn backend, nên
// việc file XUẤT HIỆN trở lại = backend HIỆN TẠI vừa ghi -> đúng cổng. Nhờ vậy
// không còn đọc nhầm cổng của lần chạy trước (nguyên nhân gây 403 do nối nhầm
// backend zombie). Chờ tối đa 90s vì cold-start nặng (nạp model) có thể ~30s+.
function waitForPortFileAndCreate(retryCount = 0) {
    if (LOCAL_MODE) { createWindow(); return; }
    const portFile = getBackendPortFile();
    if (fs.existsSync(portFile)) {
        log(`[PORT] port.json (mới) đã sẵn sàng. Khởi động window...`);
        baoKhoiDong(2, "Dịch vụ xử lý đã sẵn sàng — đang mở giao diện…");
        createWindow();
        return;
    }

    if (retryCount < 900) {
        // Đợi tối đa 90 giây (900 * 100ms) cho cold-start nặng
        if (retryCount > 0 && retryCount % 50 === 0) {
            log(`[PORT] Đang chờ backend khởi động... (${retryCount / 10}s)`);
            baoKhoiDong(1, `Đang khởi động dịch vụ xử lý… (${retryCount / 10} giây)`);
        }
        setTimeout(() => waitForPortFileAndCreate(retryCount + 1), 100);
    } else {
        log(
            `[PORT] Chờ port.json quá lâu (90s). Khởi tạo cửa sổ với cổng mặc định.`,
        );
        baoKhoiDong(2, "Dịch vụ xử lý chưa phản hồi — đang mở giao diện để báo lỗi…");
        createWindow();
    }
}

//  MỘT PHIÊN BẢN APP MỖI LÚC (07-10-2026): người dùng bấm mở app nhiều lần liên tiếp -> nhiều cửa sổ, mỗi cửa sổ một backend
//  tranh nhau port.json; tắt một cái là backend chết, các cửa sổ còn lại thành vô dụng. Lần mở thứ hai chỉ việc đưa cửa sổ
//  đang có lên trước rồi thoát ngay (chưa spawn gì nên không có gì phải dọn).
const LA_PHIEN_DAU = app.requestSingleInstanceLock();
if (!LA_PHIEN_DAU) {
    log("[INSTANCE] App đang chạy ở cửa sổ khác — thoát phiên này, đưa cửa sổ cũ lên trước.");
    app.quit();
} else {
    app.on("second-instance", () => {
        const w = (mainWindow && !mainWindow.isDestroyed()) ? mainWindow : BrowserWindow.getAllWindows()[0];
        if (w) {
            if (w.isMinimized()) w.restore();
            w.show();
            w.focus();
        }
    });
}

// Khởi chạy ứng dụng
app.whenReady().then(() => {
    if (!LA_PHIEN_DAU) return;                       // phiên trùng: đã app.quit() ở trên, không khởi động gì
    log("Electron App Ready. Khởi động các tiến trình...");
    // Bản build NỘI BỘ trỏ sang portal thử — kêu to trong log để không bao giờ
    // phát hành nhầm bản này cho khách (khách sẽ không kích hoạt được).
    if (app.isPackaged && STAGING_ADMIN_URL) {
        log(
            `[STAGING] ⚠ Bản build THỬ: đang xác thực với ${STAGING_ADMIN_URL} ` +
            `thay vì ${ADMIN_URL}. KHÔNG phát hành bản này.`,
        );
    }

    // Xoá port.json CŨ trước khi spawn backend: tránh cửa sổ đọc nhầm cổng của
    // lần chạy trước (backend cold-start chậm -> nếu còn file cũ, frontend nối
    // vào backend zombie/sai cổng -> lỗi 403 / trắng màn).
    try {
        const oldPortFile = getBackendPortFile();
        if (LOCAL_MODE) throw new Error("Local backend is managed externally; preserve its port file.");
        if (fs.existsSync(oldPortFile)) {
            fs.unlinkSync(oldPortFile);
            log("[PORT] Đã xoá port.json cũ để tránh nối nhầm cổng.");
        }
    } catch (e) {
        log(`[PORT] Không xoá được port.json cũ: ${e.message}`);
    }
    //  Màn khởi động lên TRƯỚC khi spawn backend — bấm icon là thấy app ngay.
    taoCuaSoKhoiDong();
    if (!LOCAL_MODE) killStaleBackendPorts();
    startPythonSubprocess();
    waitForPortFileAndCreate();

    //  MÁY NGỦ / KHOÁ MÀN HÌNH (08-10-2026): ghi log để có bằng chứng, báo giao diện ping lại ngay, và 3 s sau kiểm xem
    //  backend còn chạy không — chết (vd bản cũ tự tắt vì mất heartbeat) thì tự khởi động lại, không bắt người dùng tắt
    //  app bật lại. Chỉ khởi động lại khi tiến trình THẬT SỰ không còn; còn chạy mà chưa trả lời có thể đang bận.
    for (const sk of ["suspend", "lock-screen"]) powerMonitor.on(sk, () => log(`[POWER] ${sk}`));
    for (const sk of ["resume", "unlock-screen"]) {
        powerMonitor.on(sk, () => {
            log(`[POWER] ${sk}`);
            guiRenderer("app:tro-lai", { su_kien: sk });
            setTimeout(async () => {
                if (LOCAL_MODE || tatBackendChuDong || backendRestarting) return;
                if (!pyProc) {
                    log(`[POWER] Backend không còn chạy sau ${sk} — khởi động lại.`);
                    xuLyBackendThoat(null);
                    return;
                }
                const song = await kiemTraBackendSong(readBackendPort());
                log(`[POWER] Backend ${song ? "trả lời bình thường" : "chưa trả lời (có thể đang bận)"} sau ${sk}.`);
            }, 3000);
        });
    }

    app.on("activate", () => {
        if (BrowserWindow.getAllWindows().length === 0) {
            createWindow();
        }
    });
});

// Quản lý đóng ứng dụng
app.on("window-all-closed", () => {
    // Đang tái tạo cửa sổ (đổi cổng sau restart backend) -> KHÔNG thoát app.
    if (recreatingWindow) {
        log("Tất cả cửa sổ đã đóng (đang tái tạo cửa sổ — bỏ qua thoát).");
        return;
    }
    log("Tất cả cửa sổ đã đóng.");
    if (process.platform !== "darwin") {
        app.quit();
    }
});

app.on("will-quit", () => {
    log("App will-quit. Tắt các dịch vụ ngầm...");
    tatBackendChuDong = true;
    // Dọn timer định kỳ để không giữ event loop sống.
    if (versionCheckInterval) {
        clearInterval(versionCheckInterval);
        versionCheckInterval = null;
    }
    killPythonSubprocess();
    // Đóng file log để không còn handle mở giữ tiến trình lại.
    // Đặt cờ TRƯỚC khi end() để mọi log() đến muộn (vd sự kiện 'close' của backend)
    // không cố ghi vào stream đã đóng.
    logStreamEnded = true;
    try {
        logStream.end();
    } catch (e) {
        /* ignore */
    }
});
