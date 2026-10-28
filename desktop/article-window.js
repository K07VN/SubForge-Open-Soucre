"use strict";
/**
 * LẤY BÀI BÁO BẰNG CỬA SỔ TRÌNH DUYỆT CỦA APP (chế độ "Sáng tạo").
 *
 * VÌ SAO: nhiều trang tin (vd NHK News Web) dựng thân bài bằng JavaScript / chỉ đưa đủ bài cho trình duyệt thật — tải thẳng
 * từ backend chỉ được phần dẫn. Cửa sổ này nạp trang đúng như một trình duyệt trên MÁY NGƯỜI DÙNG (cùng IP, cùng công cụ
 * Chromium), đợi trang dựng xong rồi đọc HTML + chữ đang hiển thị trả về cho renderer (renderer gửi sang backend để tách bài).
 *
 * KHÔNG làm: không điền / lưu mật khẩu, không vượt cơ chế đăng nhập hay trả phí. Nếu trang đòi đăng nhập thì người dùng mở
 * cửa sổ HIỆN (``hien``) và tự đăng nhập trong trang của nhà báo, rồi bấm lấy chữ; app chỉ đọc chữ đang hiển thị.
 * Cookie nằm ở phân vùng riêng ``persist:sangtao-bai`` (không lẫn với phần khác của app).
 *
 * Chạy trong MAIN process. Cửa sổ chạy sandbox, không nodeIntegration; chỉ nhận http(s); chặn cửa sổ bật ra.
 *
 * CHỐNG SSRF (cửa sổ này chạy trên máy người dùng, cạnh backend 127.0.0.1 của app và mạng LAN): mọi request của phân vùng — kể cả chuyển hướng 302
 * và tài nguyên do trang gọi — phải qua ``kiemUrl`` (IP viết thẳng kiểm bằng số) và ``kiemDns`` (tên miền phân giải ra IP nội bộ bị chặn). Giới hạn còn lại:
 * giữa lúc kiểm và lúc nối thật vẫn có khe nhỏ nếu máy chủ DNS đổi đáp án cực nhanh (đã hẹp bằng cách dùng chung bộ phân giải + bộ nhớ đệm của Chromium,
 * nhưng không đóng hẳn nếu không đặt proxy lọc riêng).
 */
const net = require("net");
const { BrowserWindow, app } = require("electron");

const PHAN_VUNG = "persist:sangtao-bai";
const TOI_DA_CUA_SO = 3;
const TOI_DA_KY_TU_HTML = 3_000_000;
const HEN_GIO_DONG_MS = 10 * 60 * 1000;                 // cửa sổ HIỆN bị bỏ quên thì tự đóng sau 10 phút
const cuaSo = new Map();                                // id -> { win, timer }
let dem = 0;

const NOI_BO = "Không mở địa chỉ nội bộ / mạng riêng";
const KHONG_PHAN_GIAI = "Không phân giải được tên miền";

/** IPv4 dạng chấm thập phân chuẩn → [a,b,c,d]; sai dạng (số 8/16, thiếu nhóm, 0 đầu…) → null. */
function nhomIPv4(s) {
    const m = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(s);
    if (!m || m.slice(1).some((x) => x.length > 1 && x[0] === "0")) return null;
    const q = m.slice(1).map(Number);
    return q.every((x) => x <= 255) ? q : null;
}

/** IPv6 (có thể có `::`, đuôi IPv4 nhúng, vùng %…) → 8 số 16 bit; sai dạng → null. */
function nhomIPv6(s) {
    s = s.split("%")[0];
    const dv4 = /^(.*:)(\d+\.\d+\.\d+\.\d+)$/.exec(s);
    if (dv4) {
        const q = nhomIPv4(dv4[2]);
        if (!q) return null;
        s = `${dv4[1]}${((q[0] << 8) | q[1]).toString(16)}:${((q[2] << 8) | q[3]).toString(16)}`;
    }
    const phan = s.split("::");
    if (phan.length > 2) return null;
    const a = phan[0] ? phan[0].split(":") : [];
    let g;
    if (phan.length === 1) g = a;
    else {
        const b = phan[1] ? phan[1].split(":") : [];
        const thieu = 8 - a.length - b.length;
        if (thieu < 0) return null;
        g = [...a, ...Array(thieu).fill("0"), ...b];
    }
    if (g.length !== 8) return null;
    const so = g.map((x) => (/^[0-9a-f]{1,4}$/i.test(x) ? parseInt(x, 16) : NaN));
    return so.some(Number.isNaN) ? null : so;
}

/** IPv4 KHÔNG phải địa chỉ công cộng: 0/8, 10/8, 100.64/10 (CGNAT), 127/8, 169.254/16, 172.16/12, 192.0.0/24, 192.168/16, 224/4 trở lên (multicast / dành riêng / broadcast). */
function v4KhongCongCong([a, b, c]) {
    return a === 0 || a === 10 || a === 127 || (a === 100 && b >= 64 && b <= 127) || (a === 169 && b === 254) || (a === 172 && b >= 16 && b <= 31) || (a === 192 && b === 168) || (a === 192 && b === 0 && c === 0) || a >= 224;
}

/** true nếu ``ip`` (chuỗi IPv4 / IPv6) không phải địa chỉ công cộng, HOẶC không phân tích được (an toàn trước). Kiểm bằng số, không bằng chuỗi: IPv6 nhúng IPv4 (::ffff:7f00:1, NAT64, 6to4) bị giải ra rồi kiểm lại như IPv4. */
function ipKhongCongCong(ip) {
    const s = String(ip || "").trim().replace(/^\[|\]$/g, "").toLowerCase();
    const q = nhomIPv4(s);
    if (q) return v4KhongCongCong(q);
    const g = nhomIPv6(s);
    if (!g) return true;
    const nhung = (hi, lo) => [hi >> 8, hi & 255, lo >> 8, lo & 255];
    if (g.slice(0, 5).every((x) => x === 0) && g[5] === 0xffff) return v4KhongCongCong(nhung(g[6], g[7]));          // ::ffff:a.b.c.d
    if (g[0] === 0x64 && g[1] === 0xff9b && g.slice(2, 6).every((x) => x === 0)) return v4KhongCongCong(nhung(g[6], g[7])); // 64:ff9b::/96 (NAT64)
    if (g[0] === 0x2002) return v4KhongCongCong(nhung(g[1], g[2]));                                                  // 6to4
    if (g[0] === 0x2001 && (g[1] === 0 || g[1] === 0xdb8)) return true;                                              // Teredo, tài liệu
    return (g[0] & 0xe000) !== 0x2000;                                                                               // chỉ 2000::/3 là unicast toàn cầu (loại ::1, fc00::/7, fe80::/10, ff00::/8…)
}

/** Kiểm ĐỒNG BỘ phần chữ của địa chỉ: chỉ http(s); IP viết thẳng phải là địa chỉ công cộng; chặn localhost, tên một nhãn (máy trong mạng nội bộ) và đuôi nội bộ. Tên miền thường còn phải qua ``kiemDns``. */
function kiemUrl(url) {
    let u;
    try {
        u = new URL(String(url || ""));
    } catch {
        return "Đường dẫn không hợp lệ";
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") return "Chỉ nhận đường dẫn http / https";
    const h = u.hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
    if (!h) return "Đường dẫn không hợp lệ";
    if (net.isIP(h)) return ipKhongCongCong(h) ? NOI_BO : "";
    if (h === "localhost" || h.endsWith(".localhost") || /^[\d.]+$/.test(h) || /^0x/i.test(h)) return NOI_BO;
    if (!h.includes(".") || /\.(local|internal|lan|home|corp|intranet|home\.arpa)$/.test(h)) return NOI_BO;
    return "";
}

const bangDns = new Map();                              // tên miền -> { p: Promise<lý do|"">, den }
const DNS_TOI_DA = 500;
const DNS_HAN_MS = 60 * 1000;

/** Phân giải TÊN MIỀN bằng bộ phân giải của chính Chromium (cùng bộ nhớ đệm với lúc nối thật, hẹp khe DNS-rebinding) và từ chối nếu BẤT KỲ địa chỉ nào không công cộng. Trả "" nếu an toàn. */
function kiemDns(ses, host) {
    const c = bangDns.get(host);
    if (c && c.den > Date.now()) return c.p;
    const p = (async () => {
        try {
            const r = await ses.resolveHost(host);
            const ds = (r && r.endpoints ? r.endpoints : []).map((e) => e.address);
            if (!ds.length) return KHONG_PHAN_GIAI;
            return ds.some(ipKhongCongCong) ? NOI_BO : "";
        } catch {
            return KHONG_PHAN_GIAI;
        }
    })();
    if (bangDns.size >= DNS_TOI_DA) bangDns.delete(bangDns.keys().next().value);
    bangDns.set(host, { p, den: Date.now() + DNS_HAN_MS });
    return p;
}

const locDaCai = new WeakSet();
const chanLyDo = new Map();                             // webContents.id -> lý do chặn gần nhất ở KHUNG CHÍNH (để báo lỗi rõ)

/** Gắn bộ lọc lên MỌI request của phân vùng (khung chính, chuyển hướng, ảnh / script / fetch / websocket do trang gọi): mỗi địa chỉ đều qua ``kiemUrl`` + ``kiemDns`` — một trang công cộng không thể kéo cửa sổ này sang 127.0.0.1 (cổng backend của app), mạng LAN hay 169.254.169.254, kể cả qua 302 hay tên miền trỏ về IP nội bộ. */
function caiBoLoc(ses) {
    if (locDaCai.has(ses)) return;
    locDaCai.add(ses);
    ses.webRequest.onBeforeRequest({ urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] }, async (d, cb) => {
        let sai = "";
        try {
            const url = d.url.replace(/^ws/i, "http");
            sai = module.exports.kiemUrl(url);
            if (!sai) {
                const h = new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, "").replace(/\.$/, "");
                if (!net.isIP(h)) sai = await module.exports.kiemDns(ses, h);
            }
        } catch {
            sai = "Địa chỉ không hợp lệ";
        }
        if (sai && d.resourceType === "mainFrame") chanLyDo.set(d.webContentsId, sai);
        cb({ cancel: !!sai });
    });
}

/** UA của chính Chromium trong app, bỏ mã "Electron/x" và tên app (nhiều trang chặn UA lạ) — vẫn là Chromium thật, không giả trình duyệt khác. */
function uaChrome(win) {
    let ua = win.webContents.getUserAgent();
    ua = ua.replace(/\sElectron\/\S+/i, "");
    try {
        ua = ua.replace(new RegExp("\\s" + String(app.getName()).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + "\\/\\S+", "i"), "");
    } catch {
        /* giữ nguyên */
    }
    return ua;
}

function taoCuaSo(hien, host) {
    const win = new BrowserWindow({
        width: 1100,
        height: 820,
        show: !!hien,
        title: `Mở bài — ${host} (đăng nhập nếu trang yêu cầu, rồi quay lại app bấm “Lấy chữ”)`,
        autoHideMenuBar: true,
        webPreferences: { partition: PHAN_VUNG, nodeIntegration: false, contextIsolation: true, sandbox: true, backgroundThrottling: false },
    });
    win.webContents.setUserAgent(uaChrome(win));
    caiBoLoc(win.webContents.session);
    const web = (u) => /^https?:\/\//i.test(u || "");
    win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
    const chan = (e, u) => {
        if (!web(u)) e.preventDefault();
    };
    win.webContents.on("will-navigate", chan);
    win.webContents.on("will-redirect", chan);
    return win;
}

const choMs = (ms) => new Promise((r) => setTimeout(r, ms));

async function nap(win, url, tToiDa = 30000) {
    const lenh = win.loadURL(url).catch((e) => {
        // ERR_ABORTED xảy ra khi trang tự chuyển hướng — vẫn có nội dung, bỏ qua; lỗi mạng thật thì ném tiếp
        if (!/ERR_ABORTED|\(-3\)/.test(String(e && e.message))) throw e;
    });
    await Promise.race([lenh, choMs(tToiDa)]);
}

/** Đợi trang dựng xong: chữ trong trang không đổi liên tiếp ``nhip`` lần (cách nhau 1 s) và đã qua ít nhất ``toiThieuMs``; kéo xuống cuối một lần để kích hoạt phần nạp lười. */
async function choOnDinh(win, tToiDa = 22000, toiThieuMs = 2500, nhip = 3) {
    const t0 = Date.now();
    let truoc = -1;
    let giong = 0;
    let daCuon = false;
    while (Date.now() - t0 < tToiDa && !win.isDestroyed()) {
        let n = -1;
        try {
            n = await win.webContents.executeJavaScript("(document.body && document.body.innerText || '').length", true);
            if (!daCuon && Date.now() - t0 > 1200) {
                daCuon = true;
                win.webContents.executeJavaScript("window.scrollTo(0, document.body.scrollHeight)", true).catch(() => {});
            }
        } catch {
            n = -1;
        }
        giong = n === truoc && n > 0 ? giong + 1 : 0;
        truoc = n;
        if (giong >= nhip && Date.now() - t0 >= toiThieuMs) return true;
        await choMs(1000);
    }
    return false;
}

async function doc(win) {
    const wc = win.webContents;
    const html = String(await wc.executeJavaScript("document.documentElement.outerHTML", true)).slice(0, TOI_DA_KY_TU_HTML);
    const chu = String(
        await wc.executeJavaScript("(document.querySelector('main') || document.querySelector('article') || document.body || document.documentElement).innerText || ''", true),
    ).slice(0, 400000);
    return { ok: true, url: wc.getURL(), title: wc.getTitle(), html, main_text: chu, hop_thoai: await hopThoaiDangMo(wc) };
}

/** Chữ của hộp thoại ĐANG MỞ và nhìn thấy trên trang (vd NHK: "ご利用意向の確認" — xác nhận điều khoản dùng NHK ONE; hoặc mời đăng nhập), "" nếu không có.
 *  Dùng để báo cho người dùng biết vì sao trang chưa hiện đủ bài; app KHÔNG tự bấm nút nào trong hộp thoại (xác nhận điều khoản / đăng nhập là việc của người dùng). */
async function hopThoaiDangMo(wc) {
    const dem = () => {
        const ds = [...document.querySelectorAll("dialog[open], [role='dialog'], [aria-modal='true']")].filter((e) => e.getClientRects().length > 0 && getComputedStyle(e).visibility !== "hidden");
        return ds.length ? (ds[0].innerText || "").replace(/\s+/g, " ").trim().slice(0, 400) : "";
    };
    try {
        return String(await wc.executeJavaScript(`(${dem.toString()})()`, true) || "");
    } catch {
        return "";
    }
}

function dong(id) {
    const m = cuaSo.get(id);
    if (!m) return false;
    cuaSo.delete(id);
    clearTimeout(m.timer);
    try {
        if (!m.win.isDestroyed()) m.win.destroy();
    } catch {
        /* đã đóng */
    }
    return true;
}

/** Mở cửa sổ (ẩn hoặc hiện) và nạp trang. Trả {ok, id} hoặc {ok:false, error}. */
async function mo(url, { hien = false } = {}) {
    const sai = module.exports.kiemUrl(url);                 // qua module.exports để bài thử thay được bằng máy chủ cục bộ
    if (sai) return { ok: false, error: sai };
    if (cuaSo.size >= TOI_DA_CUA_SO) return { ok: false, error: "Đang mở quá nhiều cửa sổ lấy bài — hãy đóng bớt" };
    const win = taoCuaSo(hien, new URL(url).hostname);
    const wcId = win.webContents.id;
    chanLyDo.delete(wcId);
    const id = `b${++dem}`;
    const timer = setTimeout(() => dong(id), HEN_GIO_DONG_MS);
    cuaSo.set(id, { win, timer });
    const ses = win.webContents.session;
    win.on("closed", () => {
        cuaSo.delete(id);
        clearTimeout(timer);
        //  Ghi cookie xuống đĩa ngay (kể cả khi người dùng tắt app sớm): lựa chọn người dùng vừa bấm trong cửa sổ (vd xác nhận điều khoản) phải còn cho các lần lấy bài sau.
        ses.cookies.flushStore().catch(() => {});
    });
    try {
        await nap(win, url);
        return { ok: true, id };
    } catch (e) {
        dong(id);
        const ly = chanLyDo.get(wcId);
        chanLyDo.delete(wcId);
        return { ok: false, error: ly ? `Đã chặn: ${ly} (trang chuyển hướng tới địa chỉ không công cộng)` : `Không mở được trang: ${e && e.message ? e.message : e}` };
    }
}

/** Đọc cửa sổ đang mở (``cho`` = đợi trang dựng xong trước khi đọc; ``dongSau`` = đóng luôn). */
async function lay(id, { cho = false, dongSau = false } = {}) {
    const m = cuaSo.get(id);
    if (!m || m.win.isDestroyed()) return { ok: false, error: "Cửa sổ đã đóng — hãy mở lại" };
    try {
        if (cho) await choOnDinh(m.win);
        const wc = m.win.webContents;
        if (/^chrome-error:/i.test(wc.getURL())) {                    // lần nạp / chuyển hướng vừa rồi bị chặn hoặc lỗi mạng: đừng đọc trang lỗi như thể là bài
            const ly = chanLyDo.get(wc.id);
            return { ok: false, error: ly ? `Đã chặn: ${ly}` : "Trang không tải được (lỗi mạng hoặc bị chặn)" };
        }
        return await doc(m.win);
    } catch (e) {
        return { ok: false, error: `Không đọc được trang: ${e && e.message ? e.message : e}` };
    } finally {
        if (dongSau) dong(id);
    }
}

/** Một lệnh: mở cửa sổ ẨN, đợi trang dựng xong, đọc, đóng. */
async function layTuDong(url) {
    const r = await mo(url, { hien: false });
    if (!r.ok) return r;
    return lay(r.id, { cho: true, dongSau: true });
}

module.exports = { kiemUrl, kiemDns, ipKhongCongCong, mo, lay, dong, layTuDong, PHAN_VUNG };
