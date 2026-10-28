"""DANH MỤC TỆP MÔ HÌNH Qwen3-TTS — tải thẳng từ Hugging Face, giống VieNeu.

VieNeu tải ``snapshot_download`` từ HF vào ``ai_models/hf``; Qwen cũng tải từ HF (bản GGUF
của qwentts.cpp, ``Serveurperso/Qwen3-TTS-GGUF``, Apache-2.0) vào
``ai_models/qwen3_tts/models``. Khác VieNeu ở hai điểm, đều để khỏi bị đổi mô hình âm thầm:
  · GHIM REVISION: người giữ repo có đẩy bản mới thì máy khách vẫn tải đúng bản đã đo
    (bench 2026-09-23: 3 tệp Q8_0 trên máy dev khớp SHA dưới đây từng byte).
  · KIỂM SHA-256 từng tệp sau khi tải — lệch là xoá và báo lỗi, không dùng tệp hỏng.

Hai GÓI, trình tải tự chọn theo RAM (cùng ngưỡng với thang mức chạy):
  · Q8_0   ~2,1 GB — chuẩn;
  · Q4_K_M ~1,4 GB — máy RAM < QWEN_RAM_Q8_MIN_GB.
Mỗi gói = talker CustomVoice (9 giọng có sẵn) + talker Base (clone) + codec dùng chung.
"""
from __future__ import annotations

import hashlib
import os

QWEN_HF_REPO = "Serveurperso/Qwen3-TTS-GGUF"
QWEN_HF_REVISION = "b7ee2e8c7459c3bea99da23e3d178125a7d1713c"

#  tên tệp -> (số byte, sha256) — lấy NGUYÊN từ HF API /tree/<revision> (size + lfs.oid).
#  KHÔNG quy đổi tay từ MB: lệch 1 byte là tệp đúng bị coi là hỏng.
QWEN_FILES: dict[str, tuple[int, str]] = {
    "qwen-talker-0.6b-base-Q8_0.gguf":
        (992615488, "d54dbaf10591421fa764ed630d764efa717ae40cd959bd48c66d4eb1af226426"),
    "qwen-talker-0.6b-customvoice-Q8_0.gguf":
        (968588544, "4eb38675c736ed6ac72012846ac8d6ef80e5af8bc05726870f0b3a6569588519"),
    "qwen-tokenizer-12hz-Q8_0.gguf":
        (291150624, "1883beeed99348fc35e23dd225e9082f93f6f8c109330a33d935baa8acdbfd94"),
    "qwen-talker-0.6b-base-Q4_K_M.gguf":
        (628905056, "4b468ec7b1f62b90ef4ca316c0aa57deadfd54b2cf9651703ea753cedaf04226"),
    "qwen-talker-0.6b-customvoice-Q4_K_M.gguf":
        (604878080, "b3a7e6613d80f8a703c06267fc1e94d48ce91932ab82ab6e31c50f4ca4868e1e"),
    "qwen-tokenizer-12hz-Q4_K_M.gguf":
        (254974752, "cf3788b4d50aaa665fb6e57c170396aae03a3555fea52d2b5d0cda902d658039"),
}

QWEN_PACKS: dict[str, list[str]] = {
    "Q8_0": ["qwen-talker-0.6b-customvoice-Q8_0.gguf", "qwen-talker-0.6b-base-Q8_0.gguf",
             "qwen-tokenizer-12hz-Q8_0.gguf"],
    "Q4_K_M": ["qwen-talker-0.6b-customvoice-Q4_K_M.gguf", "qwen-talker-0.6b-base-Q4_K_M.gguf",
               "qwen-tokenizer-12hz-Q4_K_M.gguf"],
}

_VERIFIED_MARK = ".sha256_ok"   # <tệp>.sha256_ok ghi mã băm đã kiểm — khỏi băm lại 2 GB


def pack_mb(pack: str) -> float:
    return round(sum(QWEN_FILES[f][0] for f in QWEN_PACKS[pack]) / 1024 ** 2, 1)


def choose_pack() -> str:
    """Gói nên tải cho máy này — cùng ngưỡng RAM với thang mức chạy."""
    from utils.hardware_utils import QWEN_RAM_Q8_MIN_GB, _machine_ram_cores

    ram_gb, _ = _machine_ram_cores()
    return "Q8_0" if ram_gb >= QWEN_RAM_Q8_MIN_GB else "Q4_K_M"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(4 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def file_ok(models_dir: str, name: str, verify: bool = False) -> bool:
    """Tệp có đủ byte (và, nếu ``verify``, đúng SHA-256). Kết quả băm được ghi nhớ
    cạnh tệp, nên lần sau chỉ cần so kích thước + dấu đã kiểm."""
    size, sha = QWEN_FILES[name]
    p = os.path.join(models_dir, name)
    try:
        if os.path.getsize(p) != size:
            return False
    except OSError:
        return False
    mark = p + _VERIFIED_MARK
    try:
        with open(mark, encoding="utf-8") as f:
            if f.read().strip() == sha:
                return True
    except OSError:
        pass
    if not verify:
        return False
    if sha256_file(p) != sha:
        return False
    with open(mark, "w", encoding="utf-8") as f:
        f.write(sha)
    return True


def installed_pack(models_dir: str) -> str | None:
    """Gói ĐÃ ĐỦ trên đĩa (đủ byte + đã kiểm SHA), ưu tiên Q8_0. None = chưa có gói nào."""
    for pack in ("Q8_0", "Q4_K_M"):
        if all(file_ok(models_dir, f) for f in QWEN_PACKS[pack]):
            return pack
    return None
