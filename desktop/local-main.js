// Desktop entry point with network restrictions.
const { app, session, shell } = require("electron");
const path = require("path");
const fs = require("fs");
const root = path.resolve(__dirname, "..");
process.env.SUBFORGE_LOCAL_MODE = "1";
app.setPath("appData", path.join(root, "local-data/bootstrap"));
app.commandLine.appendSwitch("disable-background-networking");
app.commandLine.appendSwitch("disable-component-update");

function localUrl(value) {
    try {
        const u = new URL(String(value));
        return ["http:", "https:", "ws:", "wss:"].includes(u.protocol) &&
            !u.username && !u.password &&
            (u.hostname === "localhost" || u.hostname === "[::1]" || /^127\./.test(u.hostname));
    } catch (_) { return false; }
}

const originalFetch = global.fetch;
global.fetch = (url, options) => {
    const value = typeof url === "string" || url instanceof URL ? url : url.url;
    if (!localUrl(value)) return Promise.reject(new Error("Kết nối Internet đã bị tắt."));
    return originalFetch(url, options);
};
const originalOpen = shell.openExternal.bind(shell);
shell.openExternal = (url, ...args) => {
    if (!localUrl(url)) return Promise.reject(new Error("Không thể mở dịch vụ bên ngoài khi kết nối Internet đã bị tắt."));
    return originalOpen(url, ...args);
};

function guardSession(s) {
    s.webRequest.onBeforeRequest(
        { urls: ["http://*/*", "https://*/*", "ws://*/*", "wss://*/*"] },
        (details, callback) => callback({ cancel: !localUrl(details.url) }),
    );
}
app.on("session-created", guardSession);
app.whenReady().then(() => guardSession(session.defaultSession));
if (process.env.SUBFORGE_LOCAL_VERIFY_UI === "1") {
    app.on("web-contents-created", (_event, contents) => {
        contents.on("did-finish-load", async () => {
            if (!contents.getURL().endsWith("/index.html")) return;
            await new Promise(resolve => setTimeout(resolve, 5000));
            try {
                const report = await contents.executeJavaScript(`({ title: document.title, url: location.href, text: document.body.innerText, aiStatus: document.body.innerText.includes("SubForge · Trạng thái AI"), errors: window.__localErrors || [] })`);
                const out = path.join(root, "local-data/logs/checks");
                fs.mkdirSync(out, { recursive: true });
                fs.writeFileSync(path.join(out, "desktop-ui.json"), JSON.stringify(report, null, 2));
                const shot = await contents.capturePage();
                fs.writeFileSync(path.join(out, "desktop-ui.png"), shot.toPNG());
                console.log("[VERIFY] Desktop screenshot and DOM evidence saved.");
            } catch (error) { console.error("[VERIFY]", error); }
            app.quit();
        });
    });
}
require("./main.js");
