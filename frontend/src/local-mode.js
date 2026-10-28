// AI service status and availability.
addEventListener("DOMContentLoaded", () => {
    const box = document.createElement("details");
    box.style.cssText = "position:fixed;bottom:12px;right:16px;z-index:2147483647;max-width:380px;padding:10px 14px;border:1px solid #45636b;border-radius:10px;background:#102028;color:#e0f2f1;font:12px/1.6 system-ui;box-shadow:0 2px 12px #0004";
    const summary = document.createElement("summary");
    summary.textContent = "SubForge · Trạng thái AI";
    summary.style.cursor = "pointer";
    box.append(summary);
    const text = document.createElement("p");
    text.textContent = "Dịch và viết kịch bản dùng Qwen. Lồng tiếng dùng VieNeu/Qwen theo ngôn ngữ đã chọn. Đăng YouTube, tải URL và lấy bài báo trực tuyến chưa khả dụng trong chế độ không có Internet.";
    box.append(text);
    const status = document.createElement("div");
    status.textContent = "Đang kiểm tra model…";
    box.append(status);
    document.body.append(box);
    const port = window.electronAPI?.backendPort || location.port || "18080";
    fetch(`http://127.0.0.1:${port}/api/local/status`).then(r => r.json()).then(s => {
        const packs = ["whisper_base", "whisper_medium", "whisper_large_v3", "vieneu", "demucs", "qwen3_tts"];
        const installed = packs.filter(key => s.models?.[key]?.downloaded).length;
        status.textContent = (s.llm?.ready ? "Máy chủ AI sẵn sàng." : "Máy chủ AI chưa sẵn sàng — xem nhật ký trong local-data/logs.") +
            ` Model AI: ${installed}/${packs.length} gói đã cài.`;
    }).catch(() => { status.textContent = "Chưa kết nối được backend."; });
});
