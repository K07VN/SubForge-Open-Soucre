# SubForge Open Source

Mã khôi phục SubForge với adapter AI local. Runtime/model tải riêng từ [Releases](https://github.com/K07VN/SubForge-Open-Soucre/releases/tag/local-data-2026-10-28); không nằm trong lịch sử Git. Không kèm script setup, launcher hay bản build sẵn.

## Cấu trúc

```text
backend/          Python backend và adapter AI local
desktop/          Electron main/preload, IPC
frontend/src/     JavaScript và CSS khôi phục để chỉnh sửa
frontend/public/  Font, icon và tài nguyên
frontend/index.html
package.json / package-lock.json
requirements-runtime.txt / requirements-torch-cpu.txt
```

`frontend/src/app.js` là JavaScript đã format từ bundle, còn chứa React và dependency gộp. Không có source map, TSX/JSX hay cây component gốc. Việc tổ chức lại thành src không được nhận là đã khôi phục nguyên bản TSX. Python khôi phục cũng không có đủ comment/format ban đầu. Native DLL/PYD, executable và trọng số AI được cung cấp riêng; không dùng `.marshal`/`.pyz` để chạy ứng dụng.

## 1. Chuẩn bị và lấy runtime

Cần Windows x64, Node.js LTS/npm, 7-Zip và Internet để tải lần đầu. Tải **đủ** `SubForge-Local-Data.7z.001` đến phần cuối trong Releases, đặt chúng cùng thư mục, rồi dùng 7-Zip giải nén phần `.001` vào **gốc repo**. Có thể kiểm tra hash từng phần bằng:

```powershell
Get-FileHash 'C:\Downloads\SubForge-Local-Data.7z.001' -Algorithm SHA256
```

So sánh với file `SHA256SUMS.txt` trong Releases. Giữ nguyên tên và tải đủ mọi phần. Sau giải nén phải có:

```text
local-data/runtime/python312/python.exe
local-data/runtime/llama-cpp/llama-server.exe
local-data/runtime/cuda-pascal/
local-data/data/ai_models/
runtime/backend/_internal/
runtime/backend/qwen_bin/
backend/assets/jsruntime/
```

Gói gồm Python portable, model, CPU/CUDA và Qwen runtime; không chứa database, cấu hình, dự án, giọng cá nhân, token, cache hay log của máy đóng gói. Cấu hình/dữ liệu mới được tạo khi chạy. Cần khoảng 20 GB trống để giải nén, ngoài các phần nén đã tải.

## 2. Cài Electron và dựng giao diện

Mở PowerShell tại gốc repo:

```powershell
npm.cmd ci --no-audit --no-fund
New-Item -ItemType Directory -Path .\frontend\dist\assets -Force | Out-Null
Copy-Item .\frontend\public\* .\frontend\dist -Recurse -Force
Copy-Item .\frontend\src\app.js .\frontend\dist\assets\app.js -Force
Copy-Item .\frontend\src\styles.css .\frontend\dist\assets\app.css -Force
Copy-Item .\frontend\src\local-mode.js .\frontend\dist\local-mode.js -Force
$html = Get-Content .\frontend\index.html -Raw -Encoding UTF8
$html = $html.Replace('./src/app.js', './assets/app.js').Replace('./src/styles.css', './assets/app.css').Replace('./src/local-mode.js', './local-mode.js')
[IO.File]::WriteAllText((Join-Path (Get-Location) 'frontend\dist\index.html'), $html, [Text.UTF8Encoding]::new($false))
```

`dist/` là đầu ra tự sinh, không đưa lên Git. Chạy lại các lệnh copy sau khi sửa frontend. Giữ `public/` để có đủ font và icon.

## 3. Chạy local

Mở **3 cửa sổ PowerShell**, cùng đứng tại gốc repo. Khởi động theo thứ tự:

**Cửa sổ 1 — LLM local:**

```powershell
& .\local-data\runtime\llama-cpp\llama-server.exe -m .\local-data\data\ai_models\llm\qwen2.5-3b-instruct-q4_k_m.gguf --host 127.0.0.1 --port 18081 --alias subforge-local -c 32768 -ngl 0 --jinja
```

Đợi server báo sẵn sàng. LLM này chạy CPU; model AI khác có thể dùng CUDA.

**Cửa sổ 2 — backend:**

```powershell
$env:SUBFORGE_OFFLINE_ONLY = '1'
$env:SUBFORGE_LOCAL_LLM_BASE_URL = 'http://127.0.0.1:18081/v1'
$env:SUBFORGE_LOCAL_LLM_MODEL = 'subforge-local'
& .\local-data\runtime\python312\python.exe -I -B .\backend\local_main.py
```

Đợi backend sẵn sàng ở `http://127.0.0.1:18080`. Có thể kiểm tra:

```powershell
Invoke-RestMethod http://127.0.0.1:18080/api/local/status
```

**Cửa sổ 3 — giao diện desktop:**

```powershell
npx.cmd --no-install electron .
```

Dừng bằng đóng giao diện và Ctrl+C tại hai cửa sổ dịch vụ. Hai cổng 18080/18081 phải trống. Chạy offline sau khi đã tải đủ các phần runtime/model và cài Electron.

## GPU và model

Gói đã kèm overlay torch/torchaudio **2.7.1+cu126** có `sm_61`, từng kiểm tra trên GTX 1070. Máy đích cần driver NVIDIA tương thích; không cần CUDA Toolkit để chạy wheel. Qwen3-TTS dùng binary/Vulkan đi kèm. Không cam kết mọi GPU hoặc mọi tác vụ đều chạy nhanh hơn 10–20 lần.

Tắt overlay để thử CPU: đặt `$env:SUBFORGE_DISABLE_CUDA_RUNTIME='1'` trong cửa sổ backend trước khi chạy. Nếu đã dùng UI bật ép CPU, cấu hình đó được giữ trong thư mục dữ liệu.

Model gồm Qwen2.5 3B Instruct Q4_K_M, Whisper Base/Medium/Large-V3, VieNeu/MOSS, Qwen3-TTS GGUF, Demucs; OCR ONNX và FFmpeg đi kèm thư viện. Các dịch vụ cloud cũ được chuyển qua adapter local; không có mã nguồn/trọng số DeepSeek hoặc Gemini trong bản phục hồi. Chất lượng/tính năng có thể khác bản cloud.

## Cài lại dependency từ upstream — tùy chọn

Cách đã kiểm tra là dùng gói Releases. Nếu muốn tự dựng runtime Python, cài Python 3.12 x64 và chạy:

```powershell
py -3.12 -m pip install --no-compile --target .\runtime\backend\_internal -r .\requirements-runtime.txt -r .\requirements-torch-cpu.txt
```

Lệnh pip không cung cấp Python portable, Qwen executable, QuickJS hoặc các model. Các thành phần đó vẫn cần lấy từ Releases hoặc tự build/tải đúng phiên bản. Torch CPU 2.7.1 và các dependency cài lại có thể khác nguyên byte với runtime vendor; không được nhận là bản binary gốc đã khôi phục.

## Git

`.gitignore` loại `runtime/`, `local-data/`, `node_modules/`, `frontend/dist/`, model, binary và cache. Chỉ push code, assets, lockfile, requirements và README. Không đưa dữ liệu dự án/token cá nhân vào Releases. Media ngoài thư mục ứng dụng phải copy riêng nếu chuyển máy.