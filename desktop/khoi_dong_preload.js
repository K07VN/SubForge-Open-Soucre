//  Preload của MÀN KHỞI ĐỘNG (khoi_dong.html) — chỉ một việc: nút × báo main THOÁT APP qua IPC.
//  Vì sao không dùng window.close(): cửa sổ thật đang ẩn có bộ chặn "hỏi backend trước khi đóng" (mainWindow 'close'),
//  app.quit() bị nó giữ lại nên bấm × không có tác dụng (người dùng báo 08-10-2026). Main xử lý ở ipcMain "khoi-dong:thoat".
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("khoiDong", {
    thoat: () => ipcRenderer.send("khoi-dong:thoat"),
});
