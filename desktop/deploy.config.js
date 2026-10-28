// =============================================================
//  SubForge Desktop — Deployment Config
//  Sửa file này DUY NHẤT trước khi build để phân phối
// =============================================================

module.exports = {
    // URL trang Admin Portal đã deploy (Vercel, VPS, ...)
    // Dùng cho: màn hình kích hoạt (link "Chưa có API Key?")
    //           và backend để xác thực key với server trung tâm
    ADMIN_URL: "https://sub-forge-pro.vercel.app",

    // ─── CHỈ DÙNG KHI BUILD BẢN NỘI BỘ ĐỂ THỬ ─────────────────────────────
    // Ép bản ĐÓNG GÓI trỏ sang một Admin Portal khác (portal local / staging).
    //
    // Vì sao phải khai ở ĐÂY chứ không dùng biến môi trường như trước: env được
    // đọc lúc CHẠY nên người dùng cuối cũng đặt được — chỉ cần
    // `set SUBFORGE_ADMIN_URL=http://127.0.0.1:9999` trỏ về một server tự dựng
    // trả {"valid": true} là mở khoá bản quyền mà không sửa file nào. Giá trị khai
    // ở đây được "nướng" vào bản build, nên muốn đổi thì phải build lại — người
    // dùng cuối không làm được.
    //
    // ⚠ BẢN PHÁT HÀNH PHẢI ĐỂ "" — nếu quên, app sẽ xác thực với portal thử và
    //   không kích hoạt được cho khách. afterPack sẽ in CẢNH BÁO nếu khác "".
    // Ví dụ khi thử: "http://localhost:3000"
    STAGING_ADMIN_URL: "",

    // Port backend mặc định — thường không cần đổi
    PREFERRED_PY_PORT: 8000,

    // URL tải gói tăng tốc GPU. Khai ở ĐÂY (không hardcode trong Python) để mỗi lần
    // phát hành chỉ sửa 1 nơi. Backend nhận qua env SUBFORGE_GPU_PACK_URL. Trỏ đúng
    // release KHỚP phiên bản app.
    //
    // Gói GPU >4GB nên được chia thành NHIỀU part (mỗi part <1.9GB, vừa trần 2GB/file
    // của GitHub Release). Chạy `npm run electron:pack:gpu` để sinh manifest + các part
    // rồi upload TẤT CẢ lên release. URL này trỏ vào file MANIFEST (.json) — backend đọc
    // manifest để biết danh sách part và tải lần lượt.
    //   • Trỏ .json  → luồng NHIỀU PART (khuyến nghị, dùng được GitHub free).
    //   • Trỏ .zip   → luồng ZIP ĐƠN kiểu cũ (chỉ khi host không giới hạn 2GB).
    // ⚠ PHẢI ĐỔI CÙNG LÚC VỚI .exe. main.py chèn gpu_pack/_internal lên ĐẦU sys.path
    //   nên máy có gói GPU sẽ nạp `vieneu` TỪ GÓI, không phải từ .exe. Trỏ nhầm về
    //   tag cũ (v2.3.0 — gói đó còn vieneu 3.1.0) là 6 giọng mới báo "không có giọng
    //   này" trên mọi máy dùng GPU. v2.7.0 = gói đầu tiên chứa vieneu 3.3.0.
    GPU_PACK_URL:
        "https://github.com/nyatto03/SubForge-Releases/releases/download/v2.7.0/subforge_backend_gpu.manifest.json",

    // (Tuỳ chọn) CHỈ dùng cho luồng ZIP ĐƠN: SHA-256 hex của subforge_backend_gpu.zip.
    // Bỏ trống "" → backend thử tải file cạnh "<GPU_PACK_URL>.sha256" (nếu release có kèm).
    // Luồng NHIỀU PART bỏ qua giá trị này — sha256 mỗi part đã nằm trong manifest.
    GPU_PACK_SHA256: "",

    // "Phiên bản" của GÓI GPU — TÁCH BIỆT với phiên bản app.
    // Dùng để phát hiện gói GPU trên đĩa đã CŨ sau khi người dùng nâng cấp app, rồi
    // MỜI cập nhật (không tự tải lại). App chỉ báo "gói GPU cũ" khi giá trị này KHÁC
    // với giá trị đã lưu lúc cài gói.
    //
    // QUAN TRỌNG — NHIỀU BẢN APP CÓ THỂ DÙNG CHUNG 1 GÓI GPU:
    //   • Nếu bản app mới VẪN chạy được với gói GPU cũ (không đổi torch/CUDA/ABI của
    //     backend GPU) → GIỮ NGUYÊN giá trị này. App sẽ KHÔNG báo cũ, không đòi tải lại.
    //   • CHỈ tăng giá trị này khi phát hành một gói GPU MỚI KHÔNG tương thích ngược
    //     (đổi phiên bản torch/CUDA, đổi cấu trúc _internal…). Lúc đó máy đang giữ gói
    //     cũ mới được mời cập nhật.
    //   • Bỏ trống "" → TẮT hoàn toàn việc phát hiện gói cũ (không bao giờ báo outdated).
    // Không nhất thiết trùng số phiên bản app; nên đặt theo lần phát hành GÓI (vd tag
    // release chứa gói: "v2.1.0").
    // Bản gói này chứa vieneu 3.3.0 (20 giọng) — KHÔNG tương thích ngược với .exe
    // cũ và ngược lại, nên đặt tên theo nội dung gói để khỏi lệch với tag app.
    GPU_PACK_VERSION: "vieneu-3.3.0",

    // BẢO MẬT — Shared secret giữa app desktop và Admin Portal.
    // Backend desktop gửi kèm header X-SubForge-Secret khi gọi /api/verify-key,
    // và Portal BẮT BUỘC khớp giá trị này (biến môi trường SUBFORGE_DESKTOP_SECRET
    // trên Vercel). Nếu hai bên KHÔNG khớp, Portal trả 503 và app không kích hoạt được.
    //
    // ➜ HÀNH ĐỘNG TRƯỚC RELEASE: đặt biến SUBFORGE_DESKTOP_SECRET trên Vercel =
    //   ĐÚNG giá trị dưới đây (nên đổi sang giá trị bí mật riêng của bạn rồi build lại).
    DESKTOP_SECRET:
        "",

    // =============================================================
    //  DEEPSEEK — Dịch (lồng tiếng) & Tạo kịch bản (review) bằng AI
    // =============================================================
    // Cho phép dịch/biên tập trực tiếp trong app bằng model DeepSeek thay vì
    // Google Translate. Hệ thống gửi 100 đoạn/lần với prompt sẵn có; nếu 1 lô lỗi
    // sẽ TỰ chuyển lô đó về Google (chỉ áp dụng cho lồng tiếng).
    //
    // ➜ API KEY để TRỐNG ("") ở đây: NGƯỜI DÙNG tự nhập trong màn Cài đặt của app
    //   (lưu vào config.json trong thư mục dữ liệu, KHÔNG đóng gói vào bản build).
    //   Chỉ base URL + danh sách model là khai tại đây (đóng gói theo app).
    DEEPSEEK_API_KEY: "",
    DEEPSEEK_BASE_URL: "https://api.deepseek.com",
    // Backend nhận qua env SUBFORGE_DEEPSEEK_MODELS (nối bằng dấu phẩy).
    // Model V4 hiện hành (tính tới 2026-07): deepseek-v4-flash (nhanh/rẻ) &
    // deepseek-v4-pro (mạnh nhất). LƯU Ý: alias cũ deepseek-chat/deepseek-reasoner
    // BỊ NGỪNG từ 2026/07/24 — không dùng nữa.
    DEEPSEEK_MODELS: ["deepseek-v4-flash", "deepseek-v4-pro"],
    DEEPSEEK_DEFAULT_MODEL: "deepseek-v4-flash",

    // =============================================================
    //  GEMINI (Google AI Studio) — bộ AI thứ hai, cùng việc với DeepSeek
    // =============================================================
    // Gọi qua tầng tương thích OpenAI của Gemini (backend services/gemini_service.py).
    // ➜ API KEY để TRỐNG: người dùng tự tạo ở aistudio.google.com/api-keys và nhập ở
    //   Cài đặt (lưu config.json, KHÔNG đóng gói). Gói miễn phí: 5 lượt/phút và Google
    //   được dùng nội dung gửi lên để cải thiện sản phẩm — app có nhắc khi lưu key.
    // Model CHỮ ổn định (tính tới 2026-10, trang Models của AI Studio): dòng 2.5 đã ĐÓNG
    // với người dùng mới (404); bản preview không khai (giới hạn chặt, ngừng sau 2 tuần
    // báo). Ô chọn model trong app còn liệt kê THÊM model chữ mà khoá người dùng thấy.
    // Backend nhận qua env SUBFORGE_GEMINI_MODELS (nối bằng dấu phẩy).
    GEMINI_API_KEY: "",
    GEMINI_BASE_URL: "https://generativelanguage.googleapis.com/v1beta/openai",
    // Hạn mức key gói miễn phí / Google AI Pro (bảng rate-limit của tài khoản thử, 07-10-2026): Flash 5 lượt/phút · 20 lượt/NGÀY,
    // Flash-Lite 15/phút · 500/ngày → Flash-Lite làm mặc định (lô qua đêm sống được), Flash chọn tay cho kịch bản Sáng tạo.
    // Người mua API riêng (bật thanh toán) có hạn mức cao hơn nhiều — app KHÔNG chặn gì theo bảng này. Mirror gemini_service.MODELS_MAC_DINH.
    GEMINI_MODELS: ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash", "gemini-3.5-flash"],
    GEMINI_DEFAULT_MODEL: "gemini-3.5-flash-lite",
};
