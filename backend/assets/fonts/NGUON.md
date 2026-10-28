# Font đóng gói kèm app

Bộ font này đi kèm bản cài để **xem trước và video xuất ra giống nhau**.
Trước đây khung xem trước tải font từ Google CDN còn ffmpeg/libass chỉ quét
`C:/Windows/Fonts`, nên chọn Montserrat thì preview đúng mà video xuất ra lại là
Arial — không có lỗi nào báo (xem `frontend/src/constants/exportFonts.ts`).

Danh sách font hiện có: xem thẳng thư mục này. Mọi danh sách trong mã (6 nơi)
SINH TỰ ĐỘNG từ đây bằng `scripts/sinh_danh_sach_font.py` — thêm font thì chép
`.ttf` vào đây **và** `frontend/public/fonts/`, rồi chạy lại script.

Nguồn: kho `github.com/google/fonts` (thư mục `ofl/<tên họ>`), riêng
`Montserrat-Black.ttf` lấy ở `JulietaUla/Montserrat` và `Oswald-Bold.ttf` ở
`googlefonts/OswaldFont` vì google/fonts chỉ còn bản biến thiên.

Bốn họ chữ thường **Inter, Montserrat, Playfair Display, Be Vietnam Pro** (thêm
24-09-2026) gói đủ **bốn mặt** Regular / Bold / Italic / BoldItalic — tải bản TĨNH
từ Google Fonts (`fonts.googleapis.com/css2?family=…:ital,wght@0,400;0,700;1,400;1,700`
trả thẳng tệp `.ttf` tĩnh ở `fonts.gstatic.com`). Đã kiểm: không có bảng `fvar`
(không biến thiên), tên họ ID1 giống nhau ở cả bốn tệp, OS/2 weight 400/700 + cờ
nghiêng đúng, đủ dấu tiếng Việt; Inter/Montserrat/Playfair đủ chữ Kirin, Be Vietnam
Pro thì không. Script sinh danh sách tự nhận họ nhiều mặt (phải đủ đúng 4 mặt) và:
không coi là "font nặng" (nút Đậm có tác dụng), khai @font-face theo từng mặt, ghi
`BUNDLED_FONT_FACES` để chữ chèn (drawtext, một tệp) chọn đúng mặt.

Tất cả đều theo **SIL Open Font License 1.1** (`OFL.txt`) — được phép đóng gói
kèm phần mềm, kể cả phần mềm bán.

## Hai điều kiện khi thêm font mới vào đây

1. **Phải đủ glyph dấu tiếng Việt.** Đã đo từng ký tự (Ự ặ ỡ ễ ầ ớ ủ ỹ ư Ắ Ử Ị Ễ
   Ợ) bằng cách so bitmap với ô `.notdef`. Archivo Black và Lilita One trượt bài
   này nên bị loại, dù nghe rất "viral". Arial Black / Impact / Trebuchet MS có
   sẵn trong Windows cũng KHÔNG đủ dấu.
2. **Phải là mặt TĨNH, không phải font biến thiên.** libass chỉ lấy được mặt mặc
   định của font biến thiên (thường là Regular), trong khi preview chạy Chrome
   chọn được weight 900 — chữ xuất ra sẽ mảnh hơn hẳn preview. Vì thế Oswald và
   Montserrat ở đây lấy bản tĩnh Bold/Black từ kho gốc, không lấy bản `[wght]`
   trong google/fonts.

Bản sao y hệt nằm ở `frontend/public/fonts/` cho khung xem trước (@font-face
trong `frontend/src/index.css`). Thay font một bên mà quên bên kia là preview và
bản xuất lệch nhau — đúng thứ mà bộ font này sinh ra để chấm dứt.

## Zen Maru Gothic Bold (chế độ Sáng tạo — chữ tiếng Nhật)
- Tệp: `ZenMaruGothic-Bold.ttf`, giấy phép SIL OFL 1.1 (`OFL-ZenMaruGothic.txt`).
- Nguồn: Google Fonts (Zen Maru Gothic, Yoshimichi Ohira / Kosugi). Kiểm ngày: 2026-10-02.
- Chỉ backend dùng (vẽ chữ nhãn + phụ đề tiếng Nhật trong `services/sangtao_chu.py`), không nằm trong bộ font giao diện.
