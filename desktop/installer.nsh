; Custom NSIS script cho SubForge Pro
; Khi GỠ CÀI ĐẶT: hỏi người dùng có muốn xóa toàn bộ dữ liệu người dùng không
; (AI models, gpu_pack ~6GB, dự án, database, log...). Mặc định GIỮ để tránh
; xóa nhầm khi chỉ cài lại / nâng cấp.
;
; Các nơi app lưu dữ liệu NGOÀI thư mục cài đặt (electron-builder không tự dọn):
;   - C:\SubForge_Workstation_Data       : AI models + gpu_pack + flags (lớn nhất)
;   - %APPDATA%\SubForge                 : port.json + database backend
;   - %APPDATA%\SubForge Pro             : userData Electron (log, cache)

; ============================================================================
; NÂNG CẤP TẠI CHỖ (cài đè bản cũ) — customCheckAppRunning
; ============================================================================
; electron-builder mặc định CHỈ đóng "SubForge Pro.exe" trước khi cài/gỡ, nhưng
; app còn các tiến trình khác giữ file NGAY TRONG thư mục cài:
;   - subforge_backend.exe (sidecar PyInstaller, cwd đặt trong install dir; sống
;     thêm ~90-125s sau khi đóng cửa sổ, hoặc vô hạn nếu Electron crash)
;   - ffmpeg của imageio-ffmpeg (_internal\imageio_ffmpeg\binaries\ffmpeg-*.exe)
;     do backend spawn, có thể thành mồ côi chạy hàng giờ
; Khi nâng cấp, installer mới chạy uninstaller CŨ chế độ --updated (xóa atomic):
; chỉ 1 file bị khóa là abort -> "Failed to uninstall old application files".
; Macro này THAY THẾ check mặc định (chạy ở CẢ installer TRƯỚC khi gỡ bản cũ
; lẫn uninstaller), diệt hết tiến trình liên quan để nâng cấp không bao giờ kẹt.
; Số vòng "diệt rồi kiểm lại" trước khi đầu hàng và nhờ người dùng đóng tay.
!define SF_MAX_KILL_ROUNDS 4

; Ép tắt 1 tiến trình theo tên (cả cây con /t — gồm ffmpeg do backend spawn).
!macro sfKillByName IMAGE
  nsExec::Exec `%SYSTEMROOT%\System32\taskkill.exe /f /t /im "${IMAGE}"`
  Pop $0
!macroend

; Kiểm tra tiến trình còn sống. RESULT = 0 nghĩa là CÒN CHẠY (find trả 0 khi thấy).
!macro sfIsRunning IMAGE RESULT
  nsExec::Exec `%SYSTEMROOT%\System32\cmd.exe /c tasklist /FI "IMAGENAME eq ${IMAGE}" /NH | %SYSTEMROOT%\System32\find.exe /I "${IMAGE}"`
  Pop ${RESULT}
!macroend

; Lưới quét: diệt MỌI tiến trình có exe nằm trong thư mục cài (bắt được
; ffmpeg-win-x86_64-v*.exe mồ côi — tên đổi theo phiên bản imageio-ffmpeg nên
; không kill theo tên được; lọc theo đường dẫn nên không đụng ffmpeg của app
; khác trên máy). Đường dẫn + PID truyền qua BIẾN MÔI TRƯỜNG (không nhúng vào
; chuỗi lệnh) để tên thư mục chứa ký tự lạ (dấu nháy...) không phá cú pháp.
;   - Loại trừ PID của CHÍNH tiến trình này: nhánh fallback TryInPlace của
;     electron-builder chạy uninstaller ngay TRONG thư mục cài (_?=) — không
;     loại trừ thì sweep giết chính uninstaller giữa chừng.
;   - Dùng StartsWith thay -like: đường dẫn chứa ký tự wildcard ([ ]) không
;     làm sweep câm lặng vô hiệu.
!macro sfSweepInstDir
  ; Cài MỚI tinh (chưa có thư mục cài) thì không có gì để quét — bỏ qua để không
  ; tốn ~2s khởi động PowerShell một cách vô ích.
  ; Dùng ${If} của LogicLib chứ không phải Goto + label: macro này được insert
  ; nhiều lần nên label tự đặt sẽ trùng tên (NSIS báo lỗi biên dịch), còn
  ; ${__LINE__} thì ra giá trị khác nhau ở dòng Goto và dòng label.
  ${If} ${FileExists} "$INSTDIR\*.*"
    System::Call `kernel32::GetCurrentProcessId() i .r1`
    System::Call `kernel32::SetEnvironmentVariable(t "SF_SELFPID", t "$1")`
    System::Call `kernel32::SetEnvironmentVariable(t "SF_INSTDIR", t "$INSTDIR")`
    nsExec::Exec `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "if ([string]::IsNullOrWhiteSpace($$env:SF_INSTDIR)) { exit 0 }; Get-CimInstance Win32_Process | Where-Object { $$_.ExecutablePath -and $$_.ProcessId -ne [int]$$env:SF_SELFPID -and $$_.ExecutablePath.StartsWith($$env:SF_INSTDIR + '\', [System.StringComparison]::OrdinalIgnoreCase) } | ForEach-Object { Stop-Process -Id $$_.ProcessId -Force -ErrorAction SilentlyContinue }"`
    Pop $0
  ${EndIf}
!macroend

!macro customCheckAppRunning
  ; ─────────────────────────────────────────────────────────────────────────
  ; VÒNG "DIỆT → KIỂM LẠI" (2.8.0)
  ;
  ; Bản trước chỉ diệt MỘT lượt rồi Sleep 500 và đi tiếp, KHÔNG kiểm tra tiến
  ; trình đã chết thật chưa — trong khi macro mặc định của electron-builder mà
  ; nó thay thế thì có đủ verify + retry. Hệ quả: lệnh kill chưa kịp có hiệu
  ; lực (hoặc thất bại vì app chạy quyền cao / Defender đang quét backend_dist
  ; ~700MB và giữ file handle) thì uninstallOldVersion chạy ngay sau đó sẽ
  ; abort → "Failed to uninstall old application files", và người dùng phải gỡ
  ; tay mới cài được. Lỗi mang tính race nên chỉ xảy ra THỈNH THOẢNG.
  ;
  ; Không dùng label để nhảy: macro này được insert nhiều lần (installSection
  ; 2 chỗ + uninstaller), label trùng tên sẽ làm NSIS báo lỗi biên dịch. Dùng
  ; LogicLib (${Do}/${Loop}) và MessageBox nhảy tương đối (+2).
  ; ─────────────────────────────────────────────────────────────────────────
  StrCpy $R4 0
  ${Do}
    IntOp $R4 $R4 + 1

    ; Vòng đầu: xin app chính tự đóng ÊM (WM_CLOSE) để Electron chạy will-quit
    ; và tự tắt backend đúng quy trình. Chỉ làm khi app thật sự đang chạy để cài
    ; mới tinh không tốn thời gian chờ vô ích.
    ; (KHÔNG /t cho app chính: nếu Setup được mở từ trong app, /t sẽ giết cả cây
    ;  gồm chính installer.)
    ${If} $R4 == 1
      !insertmacro sfIsRunning "${APP_EXECUTABLE_FILENAME}" $R5
      ${If} $R5 == 0
        DetailPrint "Đang đóng ${PRODUCT_NAME}..."
        nsExec::Exec `%SYSTEMROOT%\System32\taskkill.exe /im "${APP_EXECUTABLE_FILENAME}"`
        Pop $0
        Sleep 2500
      ${EndIf}
    ${EndIf}

    ; Ép tắt: app chính, backend sidecar (CPU + GPU), rồi quét theo thư mục cài.
    !insertmacro sfKillByName "${APP_EXECUTABLE_FILENAME}"
    !insertmacro sfKillByName "subforge_backend.exe"
    !insertmacro sfKillByName "subforge_backend_gpu.exe"
    ; Máy chủ giọng Qwen3-TTS (tiến trình con của backend, nằm trong ai_models — NGOÀI
    ; thư mục cài nên sfSweepInstDir không quét tới). Còn chạy là giữ khoá DLL ggml.
    !insertmacro sfKillByName "subforge_qwen_tts.exe"
    !insertmacro sfSweepInstDir

    ; Chờ Windows nhả file handle. 500ms như trước là quá ngắn khi backend_dist
    ; đang bị antivirus quét — đây là nguyên nhân chính của lỗi thỉnh thoảng.
    Sleep 1500

    ; KIỂM LẠI: sạch hết thì thoát vòng.
    !insertmacro sfIsRunning "${APP_EXECUTABLE_FILENAME}" $R5
    !insertmacro sfIsRunning "subforge_backend.exe" $R6
    !insertmacro sfIsRunning "subforge_backend_gpu.exe" $R7
    !insertmacro sfIsRunning "subforge_qwen_tts.exe" $R8
    ${If} $R5 != 0
    ${AndIf} $R6 != 0
    ${AndIf} $R7 != 0
    ${AndIf} $R8 != 0
      ${ExitDo}
    ${EndIf}

    ; Hết số vòng mà vẫn còn tiến trình → gần như chắc chắn app đang chạy với
    ; quyền cao hơn installer (taskkill trả Access denied). Chỉ người dùng đóng
    ; được. Ở chế độ im lặng (/SD IDCANCEL) thì dừng để installer báo lỗi rõ
    ; ràng thay vì cài dở dang.
    ${If} $R4 >= ${SF_MAX_KILL_ROUNDS}
      DetailPrint "Không thể đóng ${PRODUCT_NAME} tự động."
      MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Không thể đóng ${PRODUCT_NAME} tự động.$\r$\n$\r$\nHãy đóng ứng dụng (kể cả biểu tượng ở khay hệ thống), rồi bấm Thử lại.$\r$\n$\r$\nNếu vẫn lỗi: mở Task Manager và kết thúc 'SubForge Pro.exe' cùng 'subforge_backend.exe'." /SD IDCANCEL IDRETRY +2
      Quit
      StrCpy $R4 0
    ${EndIf}
  ${Loop}
!macroend

; ============================================================================
; GỠ BẢN CŨ THẤT BẠI -> TỰ DỌN, KHÔNG BẮT NGƯỜI DÙNG GỠ TAY — customUnInstallCheck
; ============================================================================
; Khi cài đè, electron-builder chạy trình gỡ của BẢN CŨ ở chế độ im lặng (--updated,
; hộp "xoá dữ liệu?" tự chọn KHÔNG nhờ /SD IDNO). Trình gỡ cũ trả mã lỗi (tệp còn bị
; khoá, antivirus đang quét, trình gỡ cũ hỏng/mất) thì mặc định của electron-builder
; là hiện "Failed to uninstall old application files" rồi THOÁT — người dùng phải vào
; Settings gỡ tay, chọn No, rồi mới cài được. Móc này thay phần đó:
;   1. diệt lại mọi tiến trình của app,
;   2. tự xoá THƯ MỤC CHƯƠNG TRÌNH cũ ($INSTDIR — chỉ chứa exe/DLL/backend_dist),
;      thử lại vài lần vì antivirus nhả khoá chậm,
;   3. cài tiếp như bình thường (registry gỡ cài đặt sẽ được bản mới ghi đè).
; KHÔNG BAO GIỜ đụng dữ liệu: AI models / gpu_pack / dự án ở C:\SubForge_Workstation_Data
; (hoặc vị trí lưu người dùng chọn) và cấu hình ở %APPDATA% đều NẰM NGOÀI $INSTDIR.
; Chốt an toàn: chỉ xoá khi $INSTDIR đúng là thư mục cài của app (có exe của app) và
; đường dẫn đủ sâu — không bao giờ RMDir một ổ đĩa hay thư mục lạ.
!define SF_MAX_DON_ROUNDS 4

!macro sfDonBanCuThatBai
  ${If} ${Errors}
  ${OrIf} $R0 != 0
    DetailPrint "Gỡ bản cũ không trọn vẹn (mã $R0) — tự dọn thư mục chương trình cũ, GIỮ NGUYÊN dữ liệu."
    StrLen $R8 "$INSTDIR"
    ${If} $R8 > 12
    ${AndIf} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
      StrCpy $R6 0
      ${Do}
        IntOp $R6 $R6 + 1
        !insertmacro sfKillByName "${APP_EXECUTABLE_FILENAME}"
        !insertmacro sfKillByName "subforge_backend.exe"
        !insertmacro sfKillByName "subforge_backend_gpu.exe"
        !insertmacro sfKillByName "subforge_qwen_tts.exe"
        !insertmacro sfSweepInstDir
        Sleep 1500
        ClearErrors
        RMDir /r "$INSTDIR"
        ${IfNot} ${FileExists} "$INSTDIR\${APP_EXECUTABLE_FILENAME}"
        ${AndIfNot} ${FileExists} "$INSTDIR\resources\*.*"
          DetailPrint "Đã dọn xong thư mục chương trình cũ."
          ${ExitDo}
        ${EndIf}
        ${If} $R6 >= ${SF_MAX_DON_ROUNDS}
          MessageBox MB_RETRYCANCEL|MB_ICONEXCLAMATION "Chưa dọn được bản cũ của ${PRODUCT_NAME} vì còn tệp đang bị khoá.$\r$\n$\r$\nDữ liệu của bạn (dự án, AI models, gói GPU) KHÔNG bị ảnh hưởng.$\r$\n$\r$\nHãy đóng ${PRODUCT_NAME} (kể cả biểu tượng ở khay hệ thống) rồi bấm Thử lại. Nếu vẫn không được, khởi động lại máy rồi chạy lại bộ cài." /SD IDCANCEL IDRETRY +2
          Quit
          StrCpy $R6 0
        ${EndIf}
      ${Loop}
    ${Else}
      DetailPrint "Không thấy thư mục chương trình cũ ở $INSTDIR — cài mới đè lên."
    ${EndIf}
    ClearErrors
    StrCpy $R0 0
  ${EndIf}
!macroend

; Hai biến thể: cài theo ngữ cảnh hiện tại và cài cho riêng người dùng (per-user — bản
; SubForge Pro là per-user). electron-builder gọi SAU MỖI lượt gỡ bản cũ; gỡ êm (mã 0,
; không lỗi) thì macro không làm gì.
!macro customUnInstallCheck
  !insertmacro sfDonBanCuThatBai
!macroend
!macro customUnInstallCheckCurrentUser
  !insertmacro sfDonBanCuThatBai
!macroend

; Chọn "chỗ" cài đúng với bản đã có, ẩn trang chọn "cài cho ai":
;   - Máy đã có bản PER-MACHINE (hiếm — từng chọn "mọi người" ở biến thể assisted
;     cũ): nâng cấp per-machine tại chỗ (UAC tự elevate) — ép per-user ở đây sẽ
;     tạo 2 bản cài song song vì installer per-user không bao giờ gỡ bản HKLM.
;   - Còn lại (toàn bộ người dùng one-click cũ): ép PER-USER như trước nay.
; LƯU Ý: macro này chỉ chạy ở giao diện có trang (non-silent); cài silent đi theo
; phát hiện tự động của electron-builder (per-user nếu bản cũ per-user) — vẫn đúng.
!macro customInstallMode
  ${If} $hasPerMachineInstallation == "1"
    StrCpy $isForceMachineInstall "1"
  ${Else}
    StrCpy $isForceCurrentInstall "1"
  ${EndIf}
!macroend

!macro customUnInstall
  ; $R0 = kết quả hộp thoại. MB_YESNO | MB_DEFBUTTON2 -> nút mặc định là "No" (giữ).
  MessageBox MB_YESNO|MB_ICONQUESTION|MB_DEFBUTTON2 \
    "Bạn có muốn xóa TOÀN BỘ dữ liệu SubForge Pro không?$\r$\n$\r$\n\
Bao gồm: AI models, gói GPU (~6GB), toàn bộ dự án, cấu hình và lịch sử.$\r$\n$\r$\n\
• Chọn KHÔNG (khuyến nghị): giữ lại dữ liệu để cài lại nhanh.$\r$\n\
• Chọn CÓ: xóa sạch, giải phóng ổ đĩa (không thể hoàn tác)." \
    /SD IDNO IDYES wipeData IDNO keepData

  wipeData:
    DetailPrint "Đang xóa toàn bộ dữ liệu người dùng SubForge Pro..."
    RMDir /r "C:\SubForge_Workstation_Data"
    RMDir /r "$APPDATA\SubForge"
    RMDir /r "$APPDATA\SubForge Pro"
    DetailPrint "Đã xóa toàn bộ dữ liệu người dùng."
    ; Không hiện MessageBox ở chế độ gỡ im lặng (nâng cấp / /S).
    IfSilent doneData
    MessageBox MB_OK|MB_ICONINFORMATION \
      "Đã xóa toàn bộ dữ liệu SubForge Pro thành công.$\r$\n\
Ổ đĩa đã được giải phóng."
    Goto doneData

  keepData:
    DetailPrint "Giữ lại dữ liệu người dùng (AI models, gpu_pack, dự án)."

  doneData:
!macroend
