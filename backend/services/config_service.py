from utils.i18n import L
import os
import sys
import json
import logging

logger = logging.getLogger("ConfigService")

from models.database import get_app_data_dir
from utils.storage_paths import get_data_root

class ConfigService:
    def __init__(self):
        app_dir = get_app_data_dir()
        os.makedirs(app_dir, exist_ok=True)
        self.config_file = os.path.join(app_dir, "config.json")
        # HỢP NHẤT MỘT THƯ MỤC: dự án Lồng tiếng / Review mặc định nằm ngay trong
        # data root (cạnh ai_models / gpu_pack / app) — người dùng chỉ quản lý
        # đúng 1 thư mục. config.json cũ đã trỏ nơi khác thì vẫn được tôn trọng.
        data_root = get_data_root()
        self.defaults = {
            "dubbing_root": os.path.abspath(os.path.join(data_root, "dubbing")),
            "review_root": os.path.abspath(os.path.join(data_root, "review"))
        }
        self._ensure_config()

    def _ensure_config(self):
        if not os.path.exists(self.config_file):
            self.save_config(self.defaults)

    def load_config(self):
        """Đọc config.json. Hỏng thì trả mặc định — và GIỮ LẠI bản hỏng.

        VÌ SAO PHẢI GIỮ: tệp hỏng mà lặng lẽ trả ``self.defaults`` (đúng 2 khoá) thì
        lần LƯU tiếp theo ghi đè nguyên tệp bằng 2 khoá đó — bay sạch API key DeepSeek,
        khoá bản quyền, device_id, mọi thứ. Người dùng chỉ thấy "tự nhiên mất cấu hình"
        mà không có lỗi nào cả. Đổi tên thành ``config.hong.json`` thì còn cứu tay được,
        và log nói rõ chuyện gì vừa xảy ra.
        """
        try:
            with open(self.config_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
            raise ValueError("config.json không phải một object")
        except FileNotFoundError:
            return dict(self.defaults)
        except Exception as e:
            logger.error(f"Failed to load config: {e}")
            try:
                if os.path.getsize(self.config_file) > 0:
                    hong = self.config_file + ".hong.json"
                    os.replace(self.config_file, hong)
                    logger.error(
                        f"[cấu hình] config.json đọc không được — đã giữ bản hỏng ở "
                        f"{hong}. Cấu hình quay về mặc định."
                    )
            except OSError:
                pass
            return dict(self.defaults)

    def save_config(self, config):
        """Ghi config.json AN TOÀN: ghi ra tệp tạm rồi mới thay thế.

        Ghi thẳng (mở "w" là tệp bị cắt về rỗng ngay) mà máy tắt / app chết đúng lúc đó
        thì còn lại một tệp cụt — lần mở sau đọc không ra, cấu hình về mặc định, rồi lần
        lưu kế tiếp xoá nốt phần còn lại. ``os.replace`` là thao tác nguyên tử trên cùng
        một ổ đĩa: hoặc tệp cũ nguyên vẹn, hoặc tệp mới đủ, không có trạng thái ở giữa.
        """
        tmp = self.config_file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.config_file)
            return True
        except Exception as e:
            logger.error(f"Failed to save config: {e}")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
            return False

    def get_dubbing_root(self):
        path = self.load_config().get("dubbing_root", self.defaults["dubbing_root"])
        try:
            drive, _ = os.path.splitdrive(path)
            if drive and not os.path.exists(drive):
                # Ổ đĩa không tồn tại, tự động chuyển về mặc định ổ C
                return self.defaults["dubbing_root"]
            return path
        except:
            return self.defaults["dubbing_root"]

    def get_review_root(self):
        path = self.load_config().get("review_root", self.defaults["review_root"])
        try:
            drive, _ = os.path.splitdrive(path)
            if drive and not os.path.exists(drive):
                # Ổ đĩa không tồn tại, tự động chuyển về mặc định ổ C
                return self.defaults["review_root"]
            return path
        except:
            return self.defaults["review_root"]

    def update_roots(self, dubbing_root=None, review_root=None):
        config = self.load_config()
        if dubbing_root: config["dubbing_root"] = os.path.abspath(dubbing_root)
        if review_root: config["review_root"] = os.path.abspath(review_root)
        self.save_config(config)
        
        # Ensure directories exist
        for key in ["dubbing_root", "review_root"]:
            path = config.get(key)
            if path:
                try:
                    drive, _ = os.path.splitdrive(path)
                    if drive and not os.path.exists(drive):
                        continue
                    if not os.path.exists(path):
                        os.makedirs(path, exist_ok=True)
                except Exception as e:
                    logger.error(f"Failed to create directory {path}: {e}")
        return config

    def get_api_key(self):
        return self.load_config().get("api_key", "")

    def get_admin_url(self):
        # Ưu tiên lấy từ biến môi trường của máy chạy app (Electron truyền vào).
        env_url = os.environ.get("SUBFORGE_ADMIN_URL") or os.environ.get("ADMIN_URL")
        if env_url:
            return env_url
        # BẢO MẬT: bản đóng gói KHÔNG lấy admin_url từ config.json. File này nằm
        # trong thư mục dữ liệu người dùng (ghi được tự do), nên trước đây chỉ cần
        # sửa 1 dòng JSON là trỏ xác thực sang server giả — kể cả khi không đặt
        # được biến môi trường (vd chạy thẳng subforge_backend.exe không qua
        # Electron). Trong bản phát hành, admin_url CHỈ đến từ env do Electron cấp
        # (giá trị chốt ở deploy.config.js). Không có env → coi như chưa cấu hình.
        if getattr(sys, "frozen", False):
            return ""
        return self.load_config().get("admin_url", "")

    def update_auth_config(self, api_key=None, admin_url=None):
        config = self.load_config()
        if api_key is not None:
            config["api_key"] = api_key
        if admin_url is not None:
            config["admin_url"] = admin_url
        self.save_config(config)
        return config

    def get_device_id(self):
        """Mã định danh máy, gửi kèm mọi lần xác thực khoá bản quyền.

        THỨ TỰ CÓ CHỦ Ý — đọc kỹ trước khi sửa:

        1. ``config.json`` đã có thì TRẢ NGUYÊN, không bao giờ tính lại. Đây là
           ràng buộc sống còn: mọi máy khách đang chạy đều mang một uuid4 ngẫu
           nhiên sinh từ bản cũ, tính lại là server thấy máy lạ và trả "khoá đã
           dùng ở thiết bị khác" — khoá nhầm sạch khách đã trả tiền.
        2. Chưa có thì lấy VÂN TAY PHẦN CỨNG. Bản cũ đẻ uuid4 ngẫu nhiên, nên
           xoá thư mục dữ liệu / cài lại / đổi vị trí lưu là ra máy mới và xin
           được thêm một khoá dùng thử. Vân tay phần cứng thì cài lại vẫn thế.
        3. Không đọc được phần cứng mới rơi về uuid4 như cũ.

        KHÔNG phải tường thành: chuỗi này do máy khách tự tính rồi gửi lên, ai
        sửa config.json hoặc gọi thẳng API đều đặt được giá trị tuỳ ý. Nó chỉ
        dọn những ca vô tình và những ca lách dễ — phần còn lại dựa vào trần
        theo IP + email/SĐT duy nhất ở khâu xin khoá.
        """
        config = self.load_config()
        device_id = config.get("device_id")
        if device_id:
            return device_id

        import uuid
        raw, source = self._read_hardware_id()
        if raw:
            # Băm chứ không gửi thẳng: số máy thật (UUID bo mạch) là thông tin
            # phần cứng của khách, không cần lộ lên server để làm việc này.
            # Cắt 32 ký tự đầu rồi gói lại thành dạng UUID cho đồng dạng với
            # các device_id cũ — mọi nơi khác đang coi nó là chuỗi bất kỳ.
            import hashlib
            digest = hashlib.sha256(f"subforge-device:{raw}".encode("utf-8")).hexdigest()
            device_id = str(uuid.UUID(hex=digest[:32]))
            logger.info(f"[thiết bị] Sinh device_id từ vân tay phần cứng ({source}).")
        else:
            device_id = str(uuid.uuid4())
            logger.warning(
                "[thiết bị] Không đọc được vân tay phần cứng — dùng mã ngẫu nhiên. "
                "Máy này sẽ thành 'máy mới' nếu cài lại."
            )

        config["device_id"] = device_id
        self.save_config(config)
        return device_id

    # Giá trị rác hay gặp ở UUID bo mạch: máy lắp linh kiện lẻ / máy ảo dựng vội
    # thường để nguyên mặc định của nhà sản xuất. Lấy nhầm mấy giá trị này là
    # HÀNG NGHÌN máy khác nhau cùng một device_id — tệ hơn hẳn uuid4 ngẫu nhiên.
    _BAD_HARDWARE_IDS = {
        "00000000-0000-0000-0000-000000000000",
        "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "03000200-0400-0500-0006-000700080009",
        "default string",
        "to be filled by o.e.m.",
        "system serial number",
        "none",
        "not applicable",
    }

    def _read_hardware_id(self):
        """Trả (chuỗi vân tay, tên nguồn). Rỗng nếu không đọc được gì đáng tin.

        UUID bo mạch (SMBIOS) đứng TRƯỚC MachineGuid: MachineGuid chỉ là một giá
        trị registry, sửa bằng regedit trong 30 giây, còn SMBIOS muốn đổi trên
        máy thật thì phải nạp lại BIOS. Đổi lại, MachineGuid sống sót khi thay bo
        mạch — nên vẫn giữ làm nguồn dự phòng thay vì bỏ hẳn.
        """
        if sys.platform != "win32":
            return "", ""

        for reader, name in (
            (self._read_smbios_uuid, "UUID bo mạch"),
            (self._read_machine_guid, "MachineGuid"),
        ):
            try:
                value = (reader() or "").strip()
            except Exception as e:
                logger.warning(f"[thiết bị] Đọc {name} thất bại: {e}")
                continue
            if value and value.lower() not in self._BAD_HARDWARE_IDS:
                return value, name
            if value:
                logger.warning(f"[thiết bị] {name} là giá trị mặc định/rác — bỏ qua.")
        return "", ""

    @staticmethod
    def _read_smbios_uuid():
        import subprocess
        # timeout 10s: PowerShell lạnh trên máy yếu có thể mất vài giây. Hàm này
        # chỉ chạy ĐÚNG MỘT LẦN trong đời một lần cài, nên chờ lâu chút không sao.
        out = subprocess.check_output(
            [
                "powershell", "-NoProfile", "-NonInteractive", "-Command",
                "(Get-CimInstance -ClassName Win32_ComputerSystemProduct).UUID",
            ],
            timeout=10,
        )
        return out.decode("utf-8", errors="ignore").strip()

    @staticmethod
    def _read_machine_guid():
        import winreg
        # KEY_WOW64_64KEY: backend đóng gói có thể chạy 32-bit, không có cờ này
        # thì Windows chuyển hướng sang nhánh Wow6432Node và không thấy khoá.
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Cryptography",
            0,
            winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
        ) as k:
            return str(winreg.QueryValueEx(k, "MachineGuid")[0])

    # ------------------------------------------------------------- DeepSeek AI
    # base_url + danh sách model KHAI Ở deploy.config.js (nhận qua env). API KEY do
    # NGƯỜI DÙNG tự nhập trong Cài đặt (lưu config.json) — không đóng gói vào build.
    def get_deepseek_config(self):
        cfg = self.load_config()
        env_models = os.environ.get("SUBFORGE_DEEPSEEK_MODELS", "")
        models = [m.strip() for m in env_models.split(",") if m.strip()]
        if not models:
            models = ["deepseek-chat"]
        base_url = (
            os.environ.get("SUBFORGE_DEEPSEEK_BASE_URL")
            or "https://api.deepseek.com"
        ).rstrip("/")
        # API key: ưu tiên giá trị người dùng nhập (config.json), rồi mới đến env
        # mặc định lúc build (thường trống).
        api_key = cfg.get("deepseek_api_key") or os.environ.get(
            "SUBFORGE_DEEPSEEK_API_KEY", ""
        )
        # Model mặc định: người dùng chọn (config.json) -> env -> phần tử đầu danh sách.
        default_model = (
            cfg.get("deepseek_model")
            or os.environ.get("SUBFORGE_DEEPSEEK_DEFAULT_MODEL")
            or models[0]
        )
        if default_model not in models:
            models = [default_model] + models
        return {
            "api_key": api_key or "",
            "base_url": base_url,
            "models": models,
            "default_model": default_model,
        }

    def update_deepseek_config(self, api_key=None, default_model=None):
        config = self.load_config()
        if api_key is not None:
            config["deepseek_api_key"] = api_key.strip()
        if default_model is not None:
            config["deepseek_model"] = default_model.strip()
        self.save_config(config)
        return self.get_deepseek_config()

    # ------------------------------------------------------------- Gemini (Google AI Studio)
    # Bộ AI thứ hai (07-10-2026), cùng cách khai: base_url + model ở deploy.config.js (env), API key
    # NGƯỜI DÙNG nhập ở Cài đặt (config.json). Model mặc định của app: xem ``gemini_service``.
    def get_gemini_config(self):
        from services import gemini_service as GS

        cfg = self.load_config()
        env_models = os.environ.get("SUBFORGE_GEMINI_MODELS", "")
        models = [m.strip() for m in env_models.split(",") if m.strip()] or list(GS.MODELS_MAC_DINH)
        base_url = (os.environ.get("SUBFORGE_GEMINI_BASE_URL") or GS.BASE_URL_MAC_DINH).rstrip("/")
        api_key = cfg.get("gemini_api_key") or os.environ.get("SUBFORGE_GEMINI_API_KEY", "")
        #  Model người dùng chọn có thể NGOÀI danh sách khai sẵn (ô chọn liệt kê thêm model chữ mà khoá của họ thấy — Google ra
        #  model mới liên tục) nên KHÔNG ép về models[0] như DeepSeek; model chết thì lỗi 404 đã nêu đúng model thay thế.
        default_model = str(cfg.get("gemini_model") or os.environ.get("SUBFORGE_GEMINI_DEFAULT_MODEL") or models[0]).strip() or models[0]
        if default_model not in models:
            models = models + [default_model]
        return {"api_key": api_key, "base_url": base_url, "models": models, "default_model": default_model}

    def update_gemini_config(self, api_key=None, default_model=None):
        config = self.load_config()
        if api_key is not None:
            config["gemini_api_key"] = api_key.strip()
        if default_model is not None:
            config["gemini_model"] = default_model.strip()
        self.save_config(config)
        return self.get_gemini_config()

    # ------------------------------------------------------------- bộ AI chung (DeepSeek / Gemini)
    #  MỘT bộ "mặc định" (``ai_provider``) cho mọi nút chỉ ghi "AI" (Chi tiết, YouTube, lô mới…); nơi nào
    #  cho chọn engine thì mã ``gemini:<model>`` / ``deepseek:<model>`` tự nói bộ nào (ai_engines).
    def get_provider_config(self, provider: str) -> dict:
        from services.ai_engines import GEMINI

        return self.get_gemini_config() if provider == GEMINI else self.get_deepseek_config()

    def ai_provider_mac_dinh(self) -> str:
        """Bộ AI mặc định. Người dùng chọn rõ ở Cài đặt thì theo đó; chưa chọn: **Gemini** (người dùng dặn 07-10-2026 — tài khoản Google
        Pro phổ biến hơn), TRỪ khi máy chỉ có khoá DeepSeek (người dùng cũ chưa đụng gì) thì vẫn DeepSeek để không đổi hành vi của họ."""
        from services.ai_engines import DEEPSEEK, GEMINI, PROVIDERS

        cfg = self.load_config()
        p = str(cfg.get("ai_provider") or "").strip()
        if p in PROVIDERS:
            return p
        co_ds = bool(str(cfg.get("deepseek_api_key") or os.environ.get("SUBFORGE_DEEPSEEK_API_KEY") or "").strip())
        co_gm = bool(str(cfg.get("gemini_api_key") or os.environ.get("SUBFORGE_GEMINI_API_KEY") or "").strip())
        return DEEPSEEK if (co_ds and not co_gm) else GEMINI

    def update_ai_provider(self, provider: str) -> str:
        from services.ai_engines import PROVIDERS

        if provider not in PROVIDERS:
            raise ValueError(L(f"Bộ AI không hợp lệ: {provider}", f"Invalid AI provider: {provider}"))
        config = self.load_config()
        config["ai_provider"] = provider
        self.save_config(config)
        #  Đổi bộ mặc định thì sự cố (hết số dư / key sai) của bộ KHÔNG còn dùng phải tắt — không thì popup nhắc lại mỗi 30 phút
        #  về một bộ AI người dùng đã bỏ (góp ý 07-10-2026).
        try:
            from services.deepseek_service import xoa_su_co

            for khac in PROVIDERS:
                if khac != provider:
                    xoa_su_co(provider=khac)
        except Exception:  # noqa: BLE001
            pass
        return provider

    def ai_model_mac_dinh_cua(self, provider: str) -> str:
        """Mã model mặc định ``provider:model`` của một bộ AI (DeepSeek giữ tên trơn) — thứ mà ``deepseek_service._chat`` nhận."""
        from services.ai_engines import ma_model

        return ma_model(provider, str(self.get_provider_config(provider).get("default_model") or ""))

    def ai_model_mac_dinh(self) -> str:
        """Mã model mặc định của BỘ AI ĐANG CHỌN — thay cho ``get_deepseek_config()["default_model"]`` ở mọi nút chỉ ghi "AI"."""
        return self.ai_model_mac_dinh_cua(self.ai_provider_mac_dinh())

    @staticmethod
    def _key_hint(key: str) -> str:
        """Khoá che bớt để hiện ở Cài đặt (như phần Bản quyền): ``sk-ab…9f3c``; rỗng → ""."""
        k = str(key or "")
        if not k:
            return ""
        return f"{k[:5]}…{k[-4:]}" if len(k) > 12 else "•" * len(k)

    def get_ai_config(self) -> dict:
        """Cấu hình MỌI bộ AI cho giao diện (KHÔNG trả khoá thô)."""
        from services.ai_engines import PROVIDERS

        ra = {"default_provider": self.ai_provider_mac_dinh(), "providers": {}}
        for p in PROVIDERS:
            c = self.get_provider_config(p)
            ra["providers"][p] = {
                "base_url": c.get("base_url"), "models": c.get("models", []), "default_model": c.get("default_model"),
                "has_api_key": bool(c.get("api_key")), "key_hint": self._key_hint(c.get("api_key") or ""),
            }
        ra["ai_script"] = self.get_ai_script_options()
        return ra

    #: TẠO KỊCH BẢN BẰNG AI Ở CHẾ ĐỘ CHI TIẾT + CHẠY HÀNG LOẠT (hai đường dùng chung
    #: ``review_advanced_ai``). Chế độ Cơ bản KHÔNG đọc các mục này — giữ nhẹ.
    #:
    #:   context_enabled — BƯỚC 0: cho một model đọc TOÀN BỘ phụ đề gốc một lần, viết ghi
    #:                     chú ngữ cảnh (nhân vật, xưng hô, tên riêng, thuật ngữ) rồi gửi
    #:                     kèm MỌI lô. Không có nó, mỗi lô chỉ thấy vài chục câu -> xưng
    #:                     hô / tên riêng lệch giữa các lô. Mặc định BẬT.
    #:   context_model   — model cho bước 0. Mặc định model "pro" (bước cần suy luận nhất,
    #:                     chạy MỘT lần cho cả phim nên chênh giá không đáng kể).
    #:   summary_level   — độ dài phần TÓM TẮT cho người xem mà bước 0 viết kèm (xem lại
    #:                     ở trình dựng / chạy lô, nối vào mô tả YouTube): "co-ban" vài
    #:                     câu, "chi-tiet" vài đoạn theo trình tự video.
    #:   batch_size      — số câu gốc mỗi lô. Lô to hơn = mỗi lô thấy nhiều ngữ cảnh hơn,
    #:                     nhưng ít lô chạy song song hơn và dễ gộp/sót câu hơn.
    AI_BATCH_CHOICES = (40, 60, 80, 100, 150, 200)
    AI_BATCH_DEFAULT = 80
    AI_SUMMARY_LEVELS = ("co-ban", "chi-tiet")

    def _ai_default_context_model(self, models: list, default_model: str) -> str:
        return next((m for m in models if "pro" in m.lower()), default_model)

    def ai_models_co_the_dung(self) -> list:
        """[(mã ``provider:model``, provider)] của MỌI bộ AI đã có API key — danh sách model chọn được ở các ô "model AI"."""
        from services.ai_engines import PROVIDERS, ma_model

        ra = []
        for p in PROVIDERS:
            c = self.get_provider_config(p)
            if c.get("api_key"):
                ra += [(ma_model(p, m), p) for m in (c.get("models") or [])]
        return ra

    def get_ai_script_options(self) -> dict:
        from services.ai_engines import ma_model

        c = self.load_config()
        #  Model bước 0 là mã ``provider:model`` (DeepSeek tên trơn như trước). Đã lưu mà bộ AI đó không còn trong danh
        #  sách / chưa có khoá (bản sau bỏ tên cũ, hoặc người dùng đổi sang Gemini mà model cũ là DeepSeek không khoá)
        #  -> về model "pro" của bộ AI đang chọn, không gửi tên chết lên API.
        model = str(c.get("ai_context_model") or "").strip()
        hop_le = {m for m, _p in self.ai_models_co_the_dung()}
        if model not in hop_le:
            p = self.ai_provider_mac_dinh()
            ds = self.get_provider_config(p)
            model = ma_model(p, self._ai_default_context_model(ds.get("models") or [], ds.get("default_model") or ""))
        try:
            size = int(c.get("ai_batch_size") or self.AI_BATCH_DEFAULT)
        except (TypeError, ValueError):
            size = self.AI_BATCH_DEFAULT
        if size not in self.AI_BATCH_CHOICES:
            size = self.AI_BATCH_DEFAULT
        level = str(c.get("ai_summary_level") or "").strip()
        if level not in self.AI_SUMMARY_LEVELS:
            level = "co-ban"
        return {
            #  MẶC ĐỊNH TẮT (người dùng dặn 07-10-2026): bước 0 đọc cả phim tốn thêm một lượt dài (đo 75 s với Gemini 3.8-flash)
            #  và một lượt trong hạn mức ngày của key miễn phí / Pro; ai cần ngữ cảnh thì bật ở Cài đặt → AI.
            "context_enabled": bool(c.get("ai_context_enabled", False)),
            "context_model": model,
            "summary_level": level,
            "batch_size": size,
            "batch_choices": list(self.AI_BATCH_CHOICES),
        }

    def update_ai_script_options(self, context_enabled=None, context_model=None,
                                 batch_size=None, summary_level=None) -> dict:
        config = self.load_config()
        if context_enabled is not None:
            config["ai_context_enabled"] = bool(context_enabled)
        if context_model is not None:
            config["ai_context_model"] = str(context_model).strip()
        if summary_level is not None:
            lv = str(summary_level).strip()
            config["ai_summary_level"] = lv if lv in self.AI_SUMMARY_LEVELS else "co-ban"
        if batch_size is not None:
            try:
                size = int(batch_size)
            except (TypeError, ValueError):
                size = self.AI_BATCH_DEFAULT
            config["ai_batch_size"] = size if size in self.AI_BATCH_CHOICES else self.AI_BATCH_DEFAULT
        self.save_config(config)
        return self.get_ai_script_options()

    #: TRẦN KÝ TỰ BẢN DỊCH — cài đặt CHUNG cho cả app (cơ bản, chi tiết, chạy lô).
    #:
    #:   "chat"             — trần bằng đúng thời lượng câu gốc. Giọng đọc khớp khung
    #:                        hình, gần như không phải dồn lại.
    #:   "vua"   (mặc định) — trần rộng hơn ~20% (người dùng thêm 09-10-2026): đủ ý hơn
    #:                        "chặt" mà bước Dồn vẫn xử lý nhẹ nhàng. Mặc định cho MỌI
    #:                        chế độ, kể cả Làm nhanh.
    #:   "thoai"            — trần rộng hơn ~40%. Dịch đủ ý hơn, đổi lại phải tăng tốc
    #:                        giọng / dồn lại một chút ở bước Dồn.
    #:
    #: CỐ Ý KHÔNG có mức "bỏ hẳn trần": không trần thì AI viết dài không kiểm soát —
    #: câu dịch tràn sang câu sau, dồn kiểu gì cũng không vừa, và người dùng chỉ phát
    #: hiện sau khi đã sinh giọng cả video.
    TRAN_HE_SO = {"chat": 1.0, "vua": 1.2, "thoai": 1.4}
    TRAN_MAC_DINH = "vua"

    def get_translate_limit_mode(self) -> str:
        c = self.load_config()
        mode = str(c.get("translate_limit_mode") or "").strip()
        if mode in self.TRAN_HE_SO:
            return mode
        #  Tương thích bản trước (cờ bật/tắt): tắt -> mức thoải mái.
        if "translate_char_limit" in c:
            return "chat" if c.get("translate_char_limit") else "thoai"
        return self.TRAN_MAC_DINH

    def update_translate_limit_mode(self, mode: str) -> str:
        mode = str(mode or "").strip()
        if mode not in self.TRAN_HE_SO:
            mode = self.TRAN_MAC_DINH
        config = self.load_config()
        config["translate_limit_mode"] = mode
        config.pop("translate_char_limit", None)  # khoá cũ, không để hai nguồn
        self.save_config(config)
        return mode

    def get_translate_limit_factor(self) -> float:
        """Hệ số nhân cho trần ký tự — nơi dựng prompt dùng cái này."""
        return self.TRAN_HE_SO.get(self.get_translate_limit_mode(), 1.0)

    #: CHẤT LƯỢNG LẤY PHỤ ĐỀ (OCR + Whisper, mọi chế độ), 3 mức (người dùng chốt 30-09-2026):
    #:   "nhanh"     (như trước) — OCR 3 khung/giây, chỉ đọc khi đoán là đổi câu;
    #:               Whisper beam 1 trên CPU.
    #:   "can_bang"  (MẶC ĐỊNH) — OCR 3 khung/giây nhưng ĐỌC MỌI khung có chữ; Whisper như "nhanh"
    #:               (beam 3 đo không hơn beam 1).
    #:   "chinh_xac" — OCR 6 khung/giây, đọc mọi khung có chữ; Whisper beam 5.
    #: OCR luôn chỉ đọc trong VÙNG người dùng khoanh. Chậm hơn đổi lấy ít sót / ít nghe nhầm.
    CHAT_LUONG_SUB = ("nhanh", "can_bang", "chinh_xac")

    #: Mặc định "can_bang": đo 21 video Douyin (636 câu, vùng khoanh rộng) — nhanh sót 9,0%,
    #: can_bang 0,5%, chỉ chậm hơn ~16% (tools/bench_ocr_sot_cau.py, 30-09-2026).
    MAC_DINH_CHAT_LUONG_SUB = "can_bang"

    def get_subtitle_quality(self) -> str:
        v = str(self.load_config().get("subtitle_quality") or self.MAC_DINH_CHAT_LUONG_SUB)
        return v if v in self.CHAT_LUONG_SUB else self.MAC_DINH_CHAT_LUONG_SUB

    def update_subtitle_quality(self, value) -> str:
        v = str(value or self.MAC_DINH_CHAT_LUONG_SUB)
        config = self.load_config()
        config["subtitle_quality"] = v if v in self.CHAT_LUONG_SUB else self.MAC_DINH_CHAT_LUONG_SUB
        self.save_config(config)
        return config["subtitle_quality"]

    #: GỘP MẨU SUB NGẮN khi lấy sub gốc (services/gop_manh_ngan.py) — THỬ NGHIỆM, MẶC ĐỊNH
    #: TẮT (người dùng chốt 29-09-2026, giống công tắc iGPU). Bật bằng ô tick ở chỗ "Lấy
    #: phụ đề" (Cơ bản / Chi tiết / chạy lô) — một cài đặt chung cho mọi chế độ.
    def get_gop_manh_ngan(self) -> bool:
        return bool(self.load_config().get("gop_manh_ngan", False))

    def update_gop_manh_ngan(self, enabled) -> bool:
        config = self.load_config()
        config["gop_manh_ngan"] = bool(enabled)
        self.save_config(config)
        return config["gop_manh_ngan"]

    #: GIẢI MÃ VIDEO BẰNG GPU (iGPU) KHI XUẤT — thử nghiệm, MẶC ĐỊNH TẮT. Xem
    #: ``compositor_service._hw_decode_mode`` và số đo trong tools/bench_igpu.py.
    def get_hw_decode(self) -> bool:
        return bool(self.load_config().get("hw_decode", False))

    def update_hw_decode(self, enabled) -> bool:
        config = self.load_config()
        config["hw_decode"] = bool(enabled)
        self.save_config(config)
        return config["hw_decode"]

    #: GỢI Ý BẮT ĐẦU cho người mới (popup sau khi chọn vị trí lưu): đã xem chưa + câu
    #: trả lời (để sau này gợi ý tiếp). Lưu ở backend chứ không localStorage — không
    #: để popup hiện lại mỗi lần cài lại giao diện.
    def get_onboarding(self) -> dict:
        c = self.load_config().get("onboarding") or {}
        return {"done": bool(c.get("done")), "profile": c.get("profile") or {}}

    def update_onboarding(self, done, profile=None) -> dict:
        config = self.load_config()
        cur = config.get("onboarding") or {}
        cur["done"] = bool(done)
        if isinstance(profile, dict):
            cur["profile"] = {str(k): str(v)[:40] for k, v in list(profile.items())[:10]}
        config["onboarding"] = cur
        self.save_config(config)
        return self.get_onboarding()

    #: GIỚI THIỆU HƯỚNG DẪN TỪNG BƯỚC (frontend/src/tours) — cho MỌI người dùng, kể cả
    #: người dùng cũ (chủ dự án dặn 30-09-2026): hiện tới khi đã XEM QUA một lần (bắt đầu
    #: một chương / một luồng dùng thử). "Xem sau" không ghi gì -> lần khởi động sau nhắc
    #: lại. Lưu ở backend như ``onboarding`` (cài lại giao diện không làm hiện lại).
    def get_huong_dan(self) -> dict:
        c = self.load_config().get("huong_dan") or {}
        return {"da_xem": bool(c.get("da_xem")), "luc": c.get("luc") or 0}

    def update_huong_dan(self, da_xem: bool) -> dict:
        import time as _t

        config = self.load_config()
        config["huong_dan"] = {"da_xem": bool(da_xem), "luc": _t.time() if da_xem else 0}
        self.save_config(config)
        return self.get_huong_dan()

    def get_favorite_voices(self) -> list:
        return self.load_config().get("favorite_voices", [])

    def update_favorite_voices(self, voices: list) -> list:
        config = self.load_config()
        config["favorite_voices"] = list(voices) if isinstance(voices, (list, tuple)) else []
        self.save_config(config)
        return config["favorite_voices"]

_service = ConfigService()

def get_config_service():
    return _service


