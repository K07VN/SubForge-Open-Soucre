import asyncio
import logging
import re
from typing import List, Dict
from utils.i18n import L
from deep_translator import GoogleTranslator

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TranslationService")

# Chữ Hán (CJK Unified Ideographs + Extension A). Dùng để phát hiện bản dịch
# "hụt": Google đôi khi trả nguyên văn / dịch nửa chừng khi source='auto'.
HAN_RE = re.compile(r"[一-鿿㐀-䶿]")


def has_han(text: str) -> bool:
    return bool(HAN_RE.search(text or ""))


def _is_google_error_page(text: str) -> bool:
    """Kết quả "dịch" có thực chất là TRANG LỖI của Google không.

    Khi quá tải, Google trả nguyên trang lỗi (``Error 500 (Server Error)!!1...
    That's an error. There was an error. Please try again later.``) như một chuỗi
    dịch BÌNH THƯỜNG — không raise. Không chặn thì cụm đó thành nội dung phụ đề.

    Bắt theo DẤU HIỆU GHÉP ĐÔI (mã lỗi + câu đặc trưng của Google) thay vì chỉ một
    từ khoá, để câu thoại thật có chữ "error"/"500" không bị loại oan.

    (Bản song sinh cho tiêu đề video: download_service._is_translate_error_page —
    sửa một bên nên xem lại bên kia.)
    """
    low = (text or "").lower()
    if not low:
        return False
    has_code = re.search(r"\berror \d{3}\b", low) or "(server error)" in low
    has_phrase = (
        "that’s an error" in low
        or "that's an error" in low
        or "that’s all we know" in low
        or "that's all we know" in low
        or "please try again later" in low
    )
    return bool(has_code and has_phrase)


# Mã ngôn ngữ hợp lệ với Google — dùng chung cho cả deep_translator lẫn gtx.
_GOOGLE_LANG_MAP = {
    'zh-cn': 'zh-CN',
    'zh-tw': 'zh-TW',
    'zh': 'zh-CN',
    'ja': 'ja',
    'ko': 'ko',
    'fr': 'fr',
    'de': 'de',
    'es': 'es',
    'ru': 'ru',
    'pt': 'pt',
    'it': 'it',
    'vi': 'vi',
    'en': 'en',
}


def _gtx_translate(text: str, sl: str = "auto", tl: str = "vi") -> str:
    """Dịch qua endpoint DỰ PHÒNG ``translate.googleapis.com`` (client=gtx).

    Endpoint chính của deep_translator (``translate.google.com/m``) bị Google
    siết tần suất theo IP khá gắt — máy dùng nhiều có lúc trượt cả loạt (đo được
    8/8 lần lỗi một câu). Endpoint gtx nằm ở HẠN MỨC KHÁC nên thường vẫn chạy
    khi /m bị chặn (đo cùng thời điểm: gtx 6/6 OK). Dùng làm đường vớt, không
    thay hẳn — hai đường cùng lúc khó chết chung.

    Sync (gọi qua asyncio.to_thread như deep_translator). Lỗi -> raise.
    """
    import json
    import urllib.parse
    import urllib.request

    q = urllib.parse.urlencode(
        {"client": "gtx", "sl": sl or "auto", "tl": tl, "dt": "t", "q": text}
    )
    req = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + q,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        data = json.loads(r.read().decode("utf-8"))
    result = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    if not result or _is_google_error_page(result):
        raise ValueError("gtx không trả bản dịch hợp lệ")
    return result


#: GỘP NHIỀU ĐOẠN VÀO MỘT REQUEST — trần an toàn.
#:
#: Đo thật (2026-09-16, 30 câu tiếng Trung): gộp 1 request 1,62s so với 30 request
#: 6,8s — nhanh gấp 4 và quan trọng hơn là SỐ REQUEST giảm 30 lần, tức lâu hơn hẳn
#: mới chạm giới hạn tần suất của Google (đây mới là thứ hay làm hỏng cả lượt dịch).
#:
#: Hai trần: số dòng, và ĐỘ DÀI URL sau khi mã hoá (gtx là GET — URL quá dài thì
#: máy chủ/proxy trả 414 hoặc cắt cụt, mà cắt cụt thì dịch thiếu mà không báo lỗi).
GOP_TOI_DA_DONG = 25
GOP_TOI_DA_KY_TU = 3500


def _gtx_translate_lines(texts: list, sl: str = "auto", tl: str = "vi") -> list:
    """Dịch NHIỀU đoạn trong MỘT request: nối bằng xuống dòng rồi tách lại.

    Vì sao nối bằng ``\n`` chứ không gửi nhiều tham số ``q``: đo trên endpoint gtx
    thì nhiều ``q`` chỉ dịch cái ĐẦU TIÊN, các cái sau bị bỏ im lặng (rất dễ tưởng
    là chạy đúng). Nối bằng xuống dòng thì Google giữ nguyên số dòng.

    Raise ``ValueError`` khi số dòng trả về KHÔNG khớp số đoạn gửi đi — nơi gọi sẽ
    quay về dịch từng đoạn. Bắt buộc phải kiểm: Google thỉnh thoảng gộp hai dòng
    ngắn làm một, mà lệch một dòng là TOÀN BỘ phần sau gán nhầm câu — sai âm thầm,
    tệ hơn nhiều so với dịch chậm.
    """
    import json
    import urllib.parse
    import urllib.request

    #  Xuống dòng NẰM TRONG một đoạn sẽ phá cách tách -> quy về dấu cách.
    sach = [(t or "").replace("\r", " ").replace("\n", " ").strip() for t in texts]
    if not sach or any(not t for t in sach):
        raise ValueError("có đoạn rỗng — không gộp được")

    noi = "\n".join(sach)
    q = urllib.parse.urlencode(
        {"client": "gtx", "sl": sl or "auto", "tl": tl, "dt": "t", "q": noi}
    )
    req = urllib.request.Request(
        "https://translate.googleapis.com/translate_a/single?" + q,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        data = json.loads(r.read().decode("utf-8"))

    ghep = "".join(seg[0] for seg in (data[0] or []) if seg and seg[0])
    if not ghep or _is_google_error_page(ghep):
        raise ValueError("gtx không trả bản dịch hợp lệ")

    dong = [d.strip() for d in ghep.split("\n")]
    #  Google đôi khi thêm dòng trống ở giữa/cuối — bỏ đi rồi mới so số lượng.
    dong = [d for d in dong if d]
    if len(dong) != len(sach):
        raise ValueError(
            f"số dòng trả về ({len(dong)}) khác số đoạn gửi đi ({len(sach)})"
        )
    return dong


def _chia_lo_gop(texts: list) -> list:
    """Chia danh sách thành các lô vừa trần dòng + trần độ dài URL."""
    import urllib.parse

    lo, hien, dai = [], [], 0
    for i, t in enumerate(texts):
        n = len(urllib.parse.quote((t or "")))
        if hien and (len(hien) >= GOP_TOI_DA_DONG or dai + n > GOP_TOI_DA_KY_TU):
            lo.append(hien)
            hien, dai = [], 0
        hien.append(i)
        dai += n
    if hien:
        lo.append(hien)
    return lo


def _dest_expects_no_han(dest: str) -> bool:
    """Ngôn ngữ đích KHÔNG dùng chữ Hán (vi/en/...) — thấy Hán trong kết quả là dịch hụt.
    zh*/ja dùng chữ Hán hợp lệ nên không kiểm."""
    d = (dest or "").lower()
    return not (d.startswith("zh") or d == "ja")

class TranslationRateLimitError(Exception):
    """Raised when Google Translate returns a 429 Too Many Requests error."""
    def __init__(self, message, partial_results=None):
        super().__init__(message)
        self.partial_results = partial_results or []

class TranslationService:
    def __init__(self):
        self.supported_langs = ['vi', 'zh', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'ru', 'pt', 'it']
        self._semaphores = {}

    @property
    def semaphore(self):
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.Semaphore(10)

        if not hasattr(self, "_semaphores") or self._semaphores is None:
            self._semaphores = {}
        if loop not in self._semaphores:
            self._semaphores[loop] = asyncio.Semaphore(10)
        return self._semaphores[loop]

    def _get_translator(self, src: str, tgt: str):
        """Initialize GoogleTranslator with specific language pair."""
        s = _GOOGLE_LANG_MAP.get(src.lower(), src)
        t = _GOOGLE_LANG_MAP.get(tgt.lower(), tgt)
        return GoogleTranslator(source=s, target=t)

    async def _translate_item(self, src: str, dest: str, text: str, stop_event: asyncio.Event = None) -> str:
        """Translate a single piece of text with semaphore protection and circuit breaker.

        stop_event: cờ dừng CỤC BỘ theo từng lần batch_translate (không dùng chung
        giữa các video)."""
        if stop_event is not None and stop_event.is_set():
            return f"[Stopped: {text}]"

        clean = (text or "").strip()
        if not clean:
            return text

        async with self.semaphore:
            if stop_event is not None and stop_event.is_set():
                return f"[Stopped: {text}]"
                
            try:
                # deep-translator is synchronous, so we run it in a thread.
                #
                # GOOGLE QUÁ TẢI có HAI triệu chứng CÙNG GỐC (server trả trang lỗi
                # "Error 500!!1..."), tuỳ lúc mà biểu hiện khác nhau:
                #   1. raise "No translation was found" — parser không thấy bản
                #      dịch trong trang lỗi;
                #   2. trả NGUYÊN TRANG LỖI như một bản dịch "thành công" (không
                #      raise) — không chặn thì cụm đó thành nội dung phụ đề.
                # Trước đây mỗi triệu chứng chỉ thử lại 1 lần sau 1s — khi Google
                # lỗi dày (đo được >50% lượt gọi đơn) thì trượt cả loạt, người
                # dùng dịch 1 câu cũng "DỊCH THẤT BẠI". Nay gộp một vòng thử lại
                # có GIÃN CÁCH TĂNG DẦN (1s/2s/3s): xác suất trượt cả 4 lần thấp
                # hơn nhiều, và khoảng nghỉ cho server kịp hồi.
                gtx_sl = _GOOGLE_LANG_MAP.get(src, src)
                gtx_tl = _GOOGLE_LANG_MAP.get(dest, dest)
                result = None
                for attempt in range(4):
                    if attempt:
                        await asyncio.sleep(float(attempt))
                    # Instance MỚI cho TỪNG LẦN THỬ (không chỉ từng đoạn): instance
                    # giữ kết nối HTTP cũ nên bám đúng máy chủ Google đang lỗi —
                    # đo thực tế tái dùng instance thì cả 4 lần thử đều dính trang
                    # lỗi, trong khi instance mới qua được ngay (kết nối khác).
                    # Kiêm luôn thread-safe khi chạy song song qua asyncio.to_thread.
                    translator = self._get_translator(src, dest)
                    try:
                        result = await asyncio.to_thread(translator.translate, clean)
                        if not _is_google_error_page(result):
                            break
                        logger.warning(
                            f"Google trả trang lỗi thay vì bản dịch (lần {attempt + 1}/4): {clean[:20]}..."
                        )
                        result = None
                    except Exception as err:
                        if "no translation was found" not in str(err).lower():
                            raise
                        logger.warning(
                            f"Google không trả bản dịch (lần {attempt + 1}/4): {clean[:20]}..."
                        )
                        result = None
                    # Đường chính trượt -> thử NGAY endpoint dự phòng gtx (hạn mức
                    # khác, thường vẫn sống khi endpoint chính bị siết theo IP).
                    try:
                        result = await asyncio.to_thread(
                            _gtx_translate, clean, gtx_sl, gtx_tl
                        )
                        logger.info(
                            f"Đã dịch qua endpoint dự phòng (gtx): {clean[:20]}..."
                        )
                        break
                    except Exception as gtx_err:  # noqa: BLE001
                        logger.warning(
                            f"Endpoint dự phòng (gtx) cũng lỗi (lần {attempt + 1}/4): "
                            f"{str(gtx_err)[:60]}"
                        )
                        result = None
                if result is None:
                    logger.warning(
                        f"Google lỗi cả 4 lần (cả 2 endpoint) — đánh dấu để dịch lại sau: {clean[:20]}..."
                    )
                    return f"[Error: {text}]"


                # KHẮC PHỤC GOOGLE DỊCH "HỤT" VỚI CHỮ HÁN (source auto/zh):
                # Bắt MỌI kết quả còn sót chữ Hán khi đích là vi/en (trả nguyên văn,
                # gần-nguyên-văn thiếu 1 chữ, dịch nửa chừng...) chứ không chỉ khi
                # giống hệt 100% như trước. Thử lại zh-CN (Giản) rồi zh-TW (Phồn —
                # STT thường ra Phồn thể); vẫn còn Hán -> trả [Error:] để lần Dịch
                # sau tự chọn lại đoạn này (không lưu bản hỏng thành "đã dịch").
                if (
                    _dest_expects_no_han(dest)
                    and has_han(clean)
                    and has_han(result)
                    and (src.lower() == "auto" or src.lower().startswith("zh"))
                ):
                    best = result
                    fixed = False
                    for retry_src in ("zh-CN", "zh-TW"):
                        logger.info(
                            f"Bản dịch còn sót chữ Hán — thử lại với source='{retry_src}': {clean[:20]}..."
                        )
                        # Thử CẢ HAI endpoint cho mỗi source: khi Google /m suy
                        # (trả nguyên văn / "No translation was found") thì gtx
                        # thường vẫn dịch được — thiếu nhánh gtx ở đây từng làm
                        # cả lô rớt về [Error:] dù gtx sống (đo 2/6 vs 6/6).
                        candidates = []
                        try:
                            t_retry = self._get_translator(retry_src, dest)
                            candidates.append(
                                await asyncio.to_thread(t_retry.translate, clean)
                            )
                        except Exception as re_err:
                            logger.warning(f"Thử lại {retry_src} lỗi: {re_err}")
                        try:
                            candidates.append(
                                await asyncio.to_thread(
                                    _gtx_translate, clean, retry_src, gtx_tl
                                )
                            )
                        except Exception as gtx_err:  # noqa: BLE001
                            logger.warning(
                                f"gtx ({retry_src}) lỗi: {str(gtx_err)[:60]}"
                            )
                        for r2 in candidates:
                            if r2 and not has_han(r2) and not _is_google_error_page(r2):
                                best = r2
                                fixed = True
                                break
                            # Giữ bản "ít Hán nhất" làm phương án tốt nhất tạm thời.
                            if r2 and len(HAN_RE.findall(r2)) < len(HAN_RE.findall(best)):
                                best = r2
                        if fixed:
                            break
                    result = best
                    if has_han(result):
                        logger.warning(
                            f"Đoạn vẫn còn chữ Hán sau khi thử zh-CN/zh-TW — đánh dấu lỗi để dịch lại sau: {clean[:20]}..."
                        )
                        return f"[Error: {text}]"
                
                return result
            except Exception as e:
                err_msg = str(e).lower()
                # Detect 429 Too Many Requests
                if "429" in err_msg or "too many requests" in err_msg:
                    logger.error(f"NGHIÊM TRỌNG: Google Translate bị giới hạn tần suất (429). Đang dừng lô dịch.")
                    if stop_event is not None:
                        stop_event.set()
                    raise TranslationRateLimitError("Google Translate Rate Limit (429) hit.")

                logger.error(f"Lỗi khi dịch đoạn '{clean[:20]}...': {e}")
                return f"[Error: {text}]"

    async def batch_translate(
        self,
        texts: List[str],
        src: str = 'en',
        dest: str = 'vi',
        engine: str = 'google',
        should_stop=None,
    ) -> List[str]:
        """
        Translate a list of strings using Google Translate concurrently.
        Stops if a rate limit is hit.

        should_stop: callable không tham số (tuỳ chọn) — trả True để DỪNG dịch giữa
        chừng (dùng cho hủy THEO TỪNG VIDEO; tránh dùng cờ stop_event toàn cục vốn bị
        chia sẻ giữa các video). Kiểm giữa các lô; trả về phần đã dịch được.
        """
        if not texts:
            return []

        # Cờ dừng 429 CỤC BỘ cho riêng lần dịch này (2 video dịch song song không
        # ảnh hưởng lẫn nhau).
        stop_event = asyncio.Event()
        src = src.lower()
        dest = dest.lower()

        if src == dest:
            return list(texts)

        total = len(texts)

        # FIX: `results` must be initialized before use (previously a NameError
        # crashed every translation). Pre-allocate one slot per input so the
        # indexed tasks can write back in order.
        results: List[str] = [None] * total
        completed = 0

        # Google Translate batches are capped at 50 items; instead of rejecting
        # larger inputs we transparently split into sequential chunks of <=50.
        MAX_BATCH = 50
        logger.info(
            f"Đang dịch {total} đoạn theo từng lô {MAX_BATCH} đoạn "
            f"(giới hạn xử lý đồng thời: {self.semaphore._value})"
        )

        # We need to track indices to map results correctly
        async def indexed_task(i, text):
            nonlocal completed
            try:
                results[i] = await self._translate_item(src, dest, text, stop_event=stop_event)
                completed += 1
                if completed % 5 == 0 or completed == total:
                    logger.info(L(f"Tiến độ dịch: đã dịch {completed}/{total} đoạn ({int(completed/total*100)}%)", f"Translation progress: translated {completed}/{total} segments ({int(completed/total*100)}%)"))
            except TranslationRateLimitError:
                results[i] = f"[RateLimit: {text}]"
                # The exception is re-raised inside gather if we don't catch it,
                # but we want to know it happened.
                raise

        #  LƯỢT GỘP: thử dịch mỗi lô NHIỀU ĐOẠN TRONG MỘT REQUEST trước. Được thì
        #  số request giảm hàng chục lần (đo: 30 đoạn 1 request 1,6s so với 30
        #  request 6,8s) và lâu hơn hẳn mới chạm giới hạn tần suất của Google.
        #  Lô nào gộp hỏng (Google gộp dòng, URL quá dài, mạng lỗi) thì để nguyên
        #  cho lượt dịch từng đoạn bên dưới lo — không mất gì.
        if engine == "google" and total > 1:
            async def _gop(chi_so: list):
                if stop_event.is_set():
                    return
                doan = [texts[i] for i in chi_so]
                try:
                    ra = await asyncio.to_thread(_gtx_translate_lines, doan, src, dest)
                except Exception as e:  # noqa: BLE001 — hỏng thì quay về đường cũ
                    logger.info(f"Gộp {len(doan)} đoạn không thành ({e}) — dịch lẻ.")
                    return
                for i, v in zip(chi_so, ra):
                    results[i] = v

            cac_lo = _chia_lo_gop(texts)
            logger.info(
                f"Dịch gộp: {total} đoạn -> {len(cac_lo)} request "
                f"(tối đa {GOP_TOI_DA_DONG} đoạn/request)"
            )
            for i in range(0, len(cac_lo), 4):
                if should_stop is not None and should_stop():
                    break
                await asyncio.gather(*[_gop(x) for x in cac_lo[i:i + 4]])
            xong = sum(1 for r in results if r)
            completed = xong
            if xong:
                logger.info(f"Dịch gộp xong {xong}/{total} đoạn.")

        try:
            for start in range(0, total, MAX_BATCH):
                # Hủy theo video: dừng trước khi chạy lô kế (giữ phần đã dịch).
                if should_stop is not None and should_stop():
                    logger.info("Batch translation: dừng theo yêu cầu hủy.")
                    break
                end = min(start + MAX_BATCH, total)
                #  Bỏ qua đoạn đã có bản dịch từ lượt gộp.
                tasks = [
                    indexed_task(i, texts[i])
                    for i in range(start, end)
                    if not results[i]
                ]
                if not tasks:
                    continue
                await asyncio.gather(*tasks)
                # A rate limit trips stop_event; bail out early instead of
                # firing the remaining chunks (they would all be "[Stopped:]").
                if stop_event.is_set():
                    break
        except TranslationRateLimitError as e:
            logger.warning("Quá trình dịch bị gián đoạn do vượt giới hạn tần suất.")
            # Reuse the same exception but attach the results collected so far
            e.partial_results = results
            raise e
        except Exception as e:
            logger.error(f"Lỗi không mong muốn khi dịch theo lô: {e}")
            raise

        # LƯỢT VỚT TUẦN TỰ cho các đoạn [Error:]: dịch SONG SONG dồn cụm request
        # đúng lúc Google quá tải nên vài đoạn trượt cả 4 lần thử; chạy lại lần
        # lượt từng đoạn (thưa request, server kịp hồi) cứu được phần lớn — người
        # dùng không phải bấm Dịch thêm lượt nữa. Chặn trần 15 đoạn: Google sập
        # hẳn thì trả [Error:] sớm cho lần Dịch sau, không treo hàng phút vô ích.
        failed_idx = [
            i for i, r in enumerate(results)
            if isinstance(r, str) and r.startswith("[Error:")
        ]
        if failed_idx and len(failed_idx) <= 15 and not stop_event.is_set():
            logger.info(
                f"Vớt lại {len(failed_idx)} đoạn lỗi (chạy tuần tự, thưa request)."
            )
            for i in failed_idx:
                if should_stop is not None and should_stop():
                    break
                try:
                    retried = await self._translate_item(
                        src, dest, texts[i], stop_event=stop_event
                    )
                except TranslationRateLimitError:
                    break  # 429 -> dừng vớt, giữ [Error:] cho lần dịch sau
                if not retried.startswith(("[Error:", "[Stopped:")):
                    results[i] = retried

        return results

    async def translate_single(self, text: str, src: str, dest: str) -> str:
        """Translate a single piece of text."""
        return await self._translate_item(src, dest, text)

    def get_supported_local_pairs(self):
        """Keep compatibility for UI listing if needed."""
        return [
            {"source": s, "target": t}
            for s in self.supported_langs 
            for t in self.supported_langs if s != t
        ]