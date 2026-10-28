// LƯU Ý: KHÔNG require `shell` ở đây. Preload chạy SANDBOXED (Electron bật sandbox
// mặc định từ v20 khi nodeIntegration=false và không khai sandbox), và trong sandboxed
// preload chỉ có một tập module rất hẹp — `ipcRenderer`, `contextBridge`, `webFrame`,
// `webUtils`, `crashReporter`, `nativeImage`. `shell` là UNDEFINED.
// Trước đây preload gọi shell.openExternal() trực tiếp -> ném TypeError -> bị try/catch
// nuốt -> openExternal() luôn trả false -> BẤM LINK TRONG APP KHÔNG CÓ TÁC DỤNG (link
// liên hệ ở footer, link tải bản mới, link trong thông báo...). Nay đẩy sang main
// process qua IPC; main mới có `shell` thật.
const { contextBridge, ipcRenderer } = require("electron");

// LUU Y: `app` la module CHI CO o main process, o preload (renderer) no la
// undefined -> khong dung app.getVersion() o day. Version duoc main process
// truyen qua argv (--app-version), giong --backend-port.

// Tìm đối số --backend-port trong process.argv
const portArg = process.argv.find(arg => arg.startsWith("--backend-port="));
const backendPort = portArg ? portArg.split("=")[1] : "8000";

// Version app do main process (co app.getVersion()) truyen sang
const versionArg = process.argv.find(arg => arg.startsWith("--app-version="));
const appVersion = versionArg ? versionArg.slice("--app-version=".length) : "";

// URL Admin Portal do main process truyền (theo deploy.config / dev). Dùng để
// hiển thị link "đăng ký cấp khóa" đúng ngay cả khi backend chưa phản hồi.
// Fallback về ADMIN_URL trong deploy.config nếu arg rỗng/thiếu, đảm bảo link
// luôn đúng ngay từ lần mở đầu (single-source: electron/deploy.config.js).
let deployAdminUrl = "";
try {
    deployAdminUrl = require("./deploy.config").ADMIN_URL || "";
} catch (e) {
    /* không đọc được config -> bỏ qua */
}
const adminArg = process.argv.find(arg => arg.startsWith("--admin-url="));
const adminUrl = (adminArg ? adminArg.slice("--admin-url=".length) : "") || deployAdminUrl;

// Cầu nối bảo mật giữa Frontend (React) và Main Process (Electron)
contextBridge.exposeInMainWorld("electronAPI", {
    platform: process.platform,
    appVersion: appVersion,
    backendPort: backendPort,
    adminUrl: adminUrl,
    
    // Bạn có thể đăng ký các API IPC ở đây nếu cần gọi xuống Node.js sau này
    sendNotification: (msg) => ipcRenderer.send("notify", msg),
    // Giao diện đã vẽ khung hình đầu -> main hiện cửa sổ thật và bỏ màn khởi động (xem ipcMain "app:ui-ready").
    baoGiaoDienSanSang: () => ipcRenderer.send("app:ui-ready"),
    // Tô màu 3 nút cửa sổ (thanh tiêu đề tuỳ biến) theo chủ đề sáng / tối — xem ipcMain "titlebar:theme".
    setTitleBarTheme: (theme) => ipcRenderer.invoke("titlebar:theme", theme),
    // Toàn màn hình (F11): renderer ẩn dải tiêu đề tuỳ biến khi bật — xem main.js "cua-so:toan-man-hinh".
    laToanManHinh: () => ipcRenderer.invoke("cua-so:toan-man-hinh?"),
    onToanManHinh: (callback) => {
        ipcRenderer.removeAllListeners("cua-so:toan-man-hinh");
        ipcRenderer.on("cua-so:toan-man-hinh", (_event, bat) => callback(!!bat));
    },
    onUpdateAvailable: (callback) => {
        ipcRenderer.on("desktop-update-available", (_, payload) => callback(payload));
    },
    // Việc kiểm tra scheme nằm ở MAIN process (xem ipcMain "shell:openExternal"):
    // renderer không phải nơi đáng tin để canh cổng bảo mật, và main thì luôn có
    // `shell` thật. Ở đây chỉ chuyển tiếp.
    openExternal: (url) => ipcRenderer.invoke("shell:openExternal", url),
    showSaveVideoDialog: (options) => ipcRenderer.invoke("dialog:saveVideo", options),
    showItemInFolder: (filePath) => ipcRenderer.invoke("shell:showItemInFolder", filePath),
    // Chọn thư mục (cài đặt "Vị trí lưu trữ")
    selectDirectory: (options) => ipcRenderer.invoke("dialog:selectDirectory", options),
    // Khởi động lại backend (sau khi di chuyển dữ liệu sang vị trí mới)
    restartBackend: () => ipcRenderer.invoke("app:restartBackend"),
    // Backend thoát / đang khởi động lại / đã chạy lại (main tự canh — xem main.js xuLyBackendThoat).
    onBackendTrangThai: (callback) => {
        ipcRenderer.removeAllListeners("backend:trang-thai");
        ipcRenderer.on("backend:trang-thai", (_event, data) => callback(data));
    },
    // Máy vừa tỉnh dậy / mở khoá màn hình — giao diện ping backend ngay thay vì đợi nhịp.
    onTroLai: (callback) => {
        ipcRenderer.removeAllListeners("app:tro-lai");
        ipcRenderer.on("app:tro-lai", (_event, data) => callback(data));
    },
    kiemTraBackend: () => ipcRenderer.invoke("backend:kiem-tra"),
    // Mở cửa sổ Douyin lấy cookie mới tự động (trả về nội dung cookies.txt).
    harvestDouyinCookies: () => ipcRenderer.invoke("douyin:harvestCookies"),
    // Lấy bài báo bằng cửa sổ trình duyệt của app (trang dựng bằng JavaScript / chỉ đủ bài cho trình duyệt thật):
    //  layBaiTuDong(url)            → mở cửa sổ ẨN, đợi trang dựng xong, đọc, đóng  → {ok, url, title, html, main_text}
    //  moCuaSoBai(url, hien)        → mở cửa sổ (hien=true: người dùng nhìn / đăng nhập được) → {ok, id}
    //  docCuaSoBai(id, {cho, dongSau}) → đọc cửa sổ đang mở
    //  dongCuaSoBai(id)             → đóng
    layBaiTuDong: (url) => ipcRenderer.invoke("sangtao:baiTuDong", url),
    moCuaSoBai: (url, hien) => ipcRenderer.invoke("sangtao:baiMo", url, !!hien),
    docCuaSoBai: (id, opts) => ipcRenderer.invoke("sangtao:baiLay", id, opts || {}),
    dongCuaSoBai: (id) => ipcRenderer.invoke("sangtao:baiDong", id),
    // Nhận tiến độ thu cookie (số lượng, ttwid/msToken, giây đã trôi) mỗi ~1.5s.
    onDouyinHarvestProgress: (callback) => {
        ipcRenderer.removeAllListeners("douyin:harvestProgress");
        ipcRenderer.on("douyin:harvestProgress", (_event, data) => callback(data));
    },
});
