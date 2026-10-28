"""SCHEMA ``storyboard.json`` của chế độ "Sáng tạo" — video ĐỌC BÁO học ngoại ngữ (Anh / Nhật / Trung / Hàn…).

Chỉ còn MỘT kiểu cảnh: ``reading`` — bài đọc: các lượt đọc của 1–4 người (người dẫn + người được trích lời, có phân vai), mỗi lượt có câu + phiên âm + bản dịch.
Dữ liệu được chuẩn hoá ở :mod:`sangtao_du_lieu_hinh`. Một nơi duy nhất định nghĩa hằng số để prompt, trình phân tích, bộ dựng và giao diện
không lệch nhau. AI chỉ ĐIỀN dữ liệu, không viết mã.
"""
from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

PHIEN_BAN = 1
KIEU_HINH = ("reading",)
TY_LE = ("16:9", "9:16")
DOI_TUONG = ("general", "kids")
#: người xem cần chữ to, đọc chậm (trẻ em). "elderly" của bản cũ được đổi về "general" khi nạp.
NGUOI_XEM_CHAM = ("kids",)
MAX_CANH = 3000          # video dài vài tiếng gồm hàng trăm cảnh
MAX_LOI_DOC = 4000


class HinhCanh(BaseModel):
    model_config = ConfigDict(extra="ignore")
    type: str
    #: dữ liệu của kiểu cảnh — đã chuẩn hoá ở sangtao_du_lieu_hinh
    data: Optional[dict] = None

    @model_validator(mode="after")
    def _du_lieu(self):
        from services import sangtao_du_lieu_hinh as DL
        sach, cb = DL.chuan_hoa(self.type, self.data)
        if sach is None:
            raise ValueError(f"kiểu '{self.type}' thiếu hoặc sai dữ liệu: " + "; ".join(cb or ["không có dữ liệu"]))
        self.data = sach
        return self

    @field_validator("type")
    @classmethod
    def _kieu(cls, v: str) -> str:
        if v not in KIEU_HINH:
            raise ValueError(f"kiểu cảnh '{v}' không được hỗ trợ (chỉ còn {list(KIEU_HINH)}) — hãy tạo lại bài bằng prompt mới")
        return v


class Canh(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(ge=1)
    #: lời đọc ghép từ dữ liệu của cảnh (các câu) — chỉ để xem nhanh và ước lượng; tiếng thật lấy từ ``data``
    voiceover: str = Field(min_length=1, max_length=MAX_LOI_DOC)
    duration_s: Optional[float] = Field(default=None, ge=0.0, le=3600.0)
    visual: HinhCanh

    @field_validator("voiceover")
    @classmethod
    def _loi(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("lời đọc trống")
        return v


class Storyboard(BaseModel):
    model_config = ConfigDict(extra="ignore")
    version: int = PHIEN_BAN
    language: str = "vi"
    aspect: str = "16:9"
    audience: str = "general"
    voice: Optional[str] = None
    title: Optional[str] = Field(default=None, max_length=120)
    #: thông tin đăng YouTube do AI soạn cùng kịch bản ({tieu_de, the, hashtag, hook_anh}) — xem sangtao_youtube; không có thì None
    youtube: Optional[dict] = None
    scenes: list[Canh] = Field(min_length=1, max_length=MAX_CANH)

    @field_validator("youtube", mode="before")
    @classmethod
    def _youtube(cls, v):
        from services import sangtao_youtube as SY
        return SY.chuan_hoa(v)

    @field_validator("aspect")
    @classmethod
    def _ty_le(cls, v: str) -> str:
        if v not in TY_LE:
            raise ValueError(f"tỷ lệ '{v}' không hợp lệ, chọn một trong {list(TY_LE)}")
        return v

    @field_validator("audience", mode="before")
    @classmethod
    def _doi_tuong_cu(cls, v):
        return "general" if v == "elderly" else v

    @field_validator("audience")
    @classmethod
    def _dt(cls, v: str) -> str:
        if v not in DOI_TUONG:
            raise ValueError(f"đối tượng '{v}' không hợp lệ, chọn một trong {list(DOI_TUONG)}")
        return v

    @field_validator("version")
    @classmethod
    def _pb(cls, v: int) -> int:
        if v != PHIEN_BAN:
            raise ValueError(f"phiên bản schema {v} chưa được hỗ trợ (hiện là {PHIEN_BAN})")
        return v


def dich_loi(e: ValidationError) -> list[str]:
    """Đổi lỗi pydantic thành câu tiếng Việt có chỉ rõ cảnh/trường: 'Cảnh 3, visual.type: …'."""
    kq = []
    for er in e.errors():
        loc = list(er.get("loc") or [])
        cau = ""
        if len(loc) >= 2 and loc[0] == "scenes" and isinstance(loc[1], int):
            cau = f"Cảnh {loc[1] + 1}"
            loc = loc[2:]
        truong = ".".join(str(x) for x in loc)
        msg = str(er.get("msg", "")).replace("Value error, ", "")
        if er.get("type") == "missing":
            msg = "thiếu trường bắt buộc"
        kq.append(" — ".join(x for x in (cau, truong, msg) if x) if cau or truong else msg)
    return kq


def kiem_tra(du_lieu: dict[str, Any]) -> tuple[Optional[Storyboard], list[str]]:
    """Kiểm một dict ``storyboard`` -> (đối tượng hợp lệ | None, danh sách lỗi tiếng Việt)."""
    try:
        return Storyboard.model_validate(du_lieu), []
    except ValidationError as e:
        return None, dich_loi(e)
