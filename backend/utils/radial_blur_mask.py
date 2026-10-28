"""Tham so blur cho vung che sub (compositor export).

VUNG CHE MO DAC KIN, KHONG CO MEP MEM. File nay tung co them mask mep mem
(``write_soft_rect_mask_pgm`` + alphamerge, va ban geq du phong) de 4 vien nhat
dan; da bo han vi vien nhat lam chu goc con doc duoc o ria — nguoi dung muon che
kin ca khung. Ban soi guong o preview: ``frontend/src/utils/radialBlurMask.ts``.
"""


#: Muc mo nho nhat / lon nhat nguoi dung dat duoc. BAN SOI GUONG cua
#: ``frontend/src/utils/radialBlurMask.ts`` (BLUR_MIN / BLUR_MAX).
BLUR_MIN = 1
BLUR_MAX = 60


def _strength_norm(strength: int) -> float:
    s = max(BLUR_MIN, min(BLUR_MAX, int(strength)))
    return (s - BLUR_MIN) / float(BLUR_MAX - BLUR_MIN)


#: Thang do mo: sigma = ty le nay * CANH NGAN cua vung che.
#: BAN SOI GUONG: BLUR_SIGMA_BASE / BLUR_SIGMA_SPAN trong radialBlurMask.ts.
BLUR_SIGMA_BASE = 0.03
BLUR_SIGMA_SPAN = 0.36


def blur_sigma_ratio(strength: int) -> float:
    """Muc mo tinh theo TY LE canh ngan cua o che (khong phai pixel tuyet doi).

    VI SAO TY LE: ban cu dat ban kinh bang pixel tuyet doi, nen cung mot o che
    xuat 720p mo dam hon xuat 2K, va preview (ve tren canvas nho hon nhieu) khong
    tai nao khop duoc. Tinh theo ty le thi moi do phan giai, moi co khung xem
    truoc deu ra CUNG mot muc mo.
    """
    return BLUR_SIGMA_BASE + _strength_norm(strength) * BLUR_SIGMA_SPAN


def ffmpeg_blur_sigma(strength: int, region_w: int, region_h: int) -> float:
    """Sigma muc tieu (diem anh) cho mot vung che kich thuoc region_w x region_h."""
    min_dim = max(2, min(int(region_w), int(region_h)))
    return max(0.3, min_dim * blur_sigma_ratio(strength))


def ffmpeg_blur_luma_radius(strength: int, region_w: int = None, region_h: int = None) -> int:
    """Ban kinh boxblur (kenh luma) quy doi TU SIGMA, KEP theo kich thuoc vung.

    THANG CHAY TU "MO NHE" TOI "MO TIT" va PHAI KHOP PREVIEW. Mot lop boxblur ban
    kinh R co phuong sai ((2R+1)^2 - 1)/12; chong n lop thi phuong sai cong don, nen
    R = (sqrt(12*sigma^2/n + 1) - 1)/2. Preview dung Gaussian (CSS ``blur``) nhung
    nham CUNG mot sigma -> hai ben nhin nhu nhau.

    QUAN TRONG (giu nguyen): boxblur cua ffmpeg LOI neu radius > min(w,h)/2 — vung
    sub thuong thap nen strength cao tung lam filter fail -> export KHONG mo gi ca.
    """
    import math

    if not region_w or not region_h:
        # Khong biet kich thuoc vung: coi nhu o che chuan 1080p cao ~200px de van
        # tra ve gia tri hop ly thay vi 0.
        region_w, region_h = 1000, 200
    n = ffmpeg_blur_luma_power(strength)
    sigma = ffmpeg_blur_sigma(strength, region_w, region_h)
    r = int(round((math.sqrt(12.0 * sigma * sigma / n + 1.0) - 1.0) / 2.0))
    limit = max(2, min(int(region_w), int(region_h)) // 2 - 1)
    return max(1, min(r, limit))


def ffmpeg_blur_luma_power(strength: int) -> int:
    """So lan lap boxblur. Muc thap chi 1 luot: lap nhieu luot la mo tit ngay ca khi
    ban kinh be — dung cai lam thang do mo mat tac dung o dau thap.
    Ban soi guong: ``blurPassCount`` trong radialBlurMask.ts."""
    s = max(BLUR_MIN, min(BLUR_MAX, int(strength)))
    if s >= 45:
        return 4
    if s >= 25:
        return 3
    if s >= 10:
        return 2
    return 1


def ffmpeg_boxblur_params(strength: int, region_w: int, region_h: int) -> str:
    """Chuoi tham so boxblur day du, tach RIENG luma/chroma.

    Ly do bat buoc: cu phap ngan `boxblur=R:P` ap R cho ca kenh CHROMA — voi
    video 4:2:0, plane chroma chi bang NUA kich thuoc vung, gioi han radius
    = min(w,h)/4. Vung sub thap (vd cao 40px -> chroma limit 10) lam filter
    FAIL ("Invalid chroma_param radius") -> export khong mo gi ca. Day chinh
    la loi goc khien blur "khong an" tren cac vung thap.
    """
    r = ffmpeg_blur_luma_radius(strength, region_w, region_h)
    p = ffmpeg_blur_luma_power(strength)
    # chroma plane = 1/2 kich thuoc (4:2:0) -> gioi han an toan min(w,h)/4 - 1
    chroma_limit = max(1, min(int(region_w), int(region_h)) // 4 - 1)
    cr = max(1, min(r // 2, chroma_limit))
    return f"luma_radius={r}:luma_power={p}:chroma_radius={cr}:chroma_power={p}"


#: MEP THOA DAN cua vung che: 0 = mo DAC KIN toi tan mep (mac dinh, giu nguyen nết cu),
#: 100 = thoai dan tu tam ra het nua canh ngan. Thang theo TY LE canh vung nen doi do
#: phan giai xuat khong lam mep day/mong khac di.
#: BAN SOI GUONG: ``featherBandPx`` trong frontend/src/utils/radialBlurMask.ts.
FEATHER_MIN = 0
FEATHER_MAX = 100


def feather_band_px(feather: int, region_w: int, region_h: int) -> float:
    """Be rong dai chuyen tiep (diem anh) o MOI mep cua vung che."""
    f = max(FEATHER_MIN, min(FEATHER_MAX, int(feather or 0)))
    if f <= 0:
        return 0.0
    return (f / 100.0) * min(int(region_w), int(region_h)) / 2.0


def feather_alpha_expr(feather: int, region_w: int, region_h: int) -> str:
    """Bieu thuc ``geq`` cho kenh ALPHA cua mieng da lam mo, hoac "" neu khong thoa.

    HINH DANG: chu nhat BO TRON 4 GOC roi thoai dan — giua canh la duong THANG (khong
    cong), chi 4 goc luon cung ban kinh bang dai thoai. Doc = khoang cach tu diem anh
    toi vien cua hinh bo tron do, chia cho dai thoai.

    Truoc day dung TICH cua hai ram ngang/doc (tach rieng tung truc). Cach do re nhung
    4 goc nhat rat nhanh va duong dong muc cong ra hinh hyperbol — nhin ra goc nhon chu
    khong phai goc bo. BAN SOI GUONG: ``featherMaskCanvas`` trong radialBlurMask.ts.
    """
    band = feather_band_px(feather, region_w, region_h)
    if band < 1:
        return ""
    w = max(1, int(region_w))
    h = max(1, int(region_h))
    #  Nua canh + ban kinh bo goc. Ban kinh khong duoc vuot qua nua canh ngan, khong
    #  thi o che be se bo tron thanh hinh vien thuoc.
    ax = (w - 1) / 2.0
    ay = (h - 1) / 2.0
    r = min(band, ax, ay)
    #  Khoang cach tu TAM ra, va phan vuot qua doan THANG cua moi truc.
    cx = f"abs(X-{ax:.3f})"
    cy = f"abs(Y-{ay:.3f})"
    qx = f"({cx}-{ax - r:.3f})"
    qy = f"({cy}-{ay - r:.3f})"
    #  Trong goc (ca hai deu vuot) thi do theo CUNG TRON; con lai do theo canh thang.
    dist = (
        f"if(gt({qx},0)*gt({qy},0),"
        f"{r:.3f}-hypot({qx},{qy}),"
        f"min({ax:.3f}-{cx},{ay:.3f}-{cy}))"
    )
    return f"255*clip({dist}/{band:.3f},0,1)"
