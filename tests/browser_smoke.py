"""用 Chromium DevTools 协议做浏览器冒烟并保存截图，不依赖第三方包。"""
import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

from tests.support import DemoServer, register
import config


def chrome_binary():
    candidates = [
        os.path.expanduser("~/.cache/ms-playwright/chromium-1254/chrome-linux64/chrome"),
        os.path.expanduser("~/.cache/ms-playwright/chromium-1234/chrome-linux64/chrome"),
        "/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
    ]
    for path in candidates:
        if os.path.exists(path):
            return path
    raise RuntimeError("chromium not found")


class WS:
    def __init__(self, url):
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=20)
        key = base64.b64encode(os.urandom(16)).decode()
        request = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\n"
                   "Connection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n"
                   % (parsed.path or "/", parsed.hostname, parsed.port, key))
        self.sock.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.sock.recv(4096)
        if b" 101 " not in data.split(b"\r\n", 1)[0]:
            raise RuntimeError("websocket handshake failed")

    def _read(self, size):
        data = b""
        while len(data) < size:
            chunk = self.sock.recv(size - len(data))
            if not chunk:
                raise RuntimeError("websocket closed")
            data += chunk
        return data

    def send(self, text):
        payload = text.encode("utf-8")
        mask = os.urandom(4)
        length = len(payload)
        header = b"\x81"
        if length < 126:
            header += bytes([0x80 | length])
        elif length < 65536:
            header += bytes([0x80 | 126]) + struct.pack("!H", length)
        else:
            header += bytes([0x80 | 127]) + struct.pack("!Q", length)
        masked = bytes(payload[i] ^ mask[i % 4] for i in range(length))
        self.sock.sendall(header + mask + masked)

    def receive(self):
        fragments = b""
        while True:
            first, second = self._read(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read(8))[0]
            if second & 0x80:
                mask = self._read(4)
            else:
                mask = None
            payload = self._read(length)
            if mask:
                payload = bytes(payload[i] ^ mask[i % 4] for i in range(length))
            if opcode == 0x9:
                self.send(payload.decode("utf-8", "ignore"))
                continue
            if opcode == 0x8:
                raise RuntimeError("websocket closed by peer")
            fragments += payload
            if first & 0x80:
                return fragments.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class CDP:
    def __init__(self, ws_url):
        self.ws = WS(ws_url)
        self.counter = 0

    def call(self, method, params=None):
        self.counter += 1
        message_id = self.counter
        self.ws.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))
        while True:
            data = json.loads(self.ws.receive())
            if data.get("id") == message_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result", {})

    def evaluate(self, expression):
        return self.call("Runtime.evaluate", {"expression": expression, "awaitPromise": True,
                                              "returnByValue": True})

    def navigate(self, url):
        self.call("Page.navigate", {"url": url})
        deadline = time.time() + 10
        while time.time() < deadline:
            result = self.evaluate("document.readyState")
            if result.get("result", {}).get("value") == "complete":
                time.sleep(0.5)
                return
            time.sleep(0.15)
        raise RuntimeError("page did not finish loading: " + url)

    def screenshot(self, path):
        result = self.call("Page.captureScreenshot", {"format": "png", "captureBeyondViewport": False})
        with open(path, "wb") as handle:
            handle.write(base64.b64decode(result["data"]))

    def value(self, expression):
        return self.evaluate(expression).get("result", {}).get("value")


def open_target(port, url):
    encoded = urllib.parse.quote(url, safe=":/")
    request = urllib.request.Request("http://127.0.0.1:%d/json/new?%s" % (port, encoded), method="PUT")
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read().decode("utf-8"))


def run(evidence_dir):
    os.makedirs(evidence_dir, exist_ok=True)
    with DemoServer() as server:
        admin = server.admin_token()
        code, student = register(server, "2024001014")
        assert code == 201, student
        code, teacher = register(server, "2024001015")
        assert code == 201, teacher
        pilot = config.PILOT_ORG["id"]
        members = server.get("/api/orgs/%s/members" % pilot, admin)[1]["members"]
        teacher_row = next(item for item in members if item["studentId"] == "2024001015")
        server.patch("/api/orgs/%s/members/%s" % (pilot, teacher_row["userId"]), {"role": "org_admin"}, admin)
        server.post("/api/consume", {"amount": 5000, "requestId": "browser-consume"}, student["token"])
        code, work = server.post("/api/works", {"title": "浏览器冒烟作品", "summary": "用于检查教师审核卡片。", "category": "学习"},
                                 student["token"])
        assert code == 201, work

        profile = tempfile.mkdtemp(prefix="campus-chrome-")
        chrome = subprocess.Popen([chrome_binary(), "--headless=new", "--no-sandbox", "--disable-gpu",
                                   "--disable-dev-shm-usage", "--remote-debugging-port=0",
                                   "--user-data-dir=" + profile, "about:blank"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            port_file = os.path.join(profile, "DevToolsActivePort")
            deadline = time.time() + 15
            while not os.path.exists(port_file) and time.time() < deadline:
                time.sleep(0.1)
            with open(port_file, "r", encoding="utf-8") as handle:
                port = int(handle.readline().strip())
            target = open_target(port, server.base + "/index.html")
            cdp = CDP(target["webSocketDebuggerUrl"])
            cdp.call("Page.enable")
            cdp.call("Runtime.enable")
            cdp.call("Emulation.setDeviceMetricsOverride", {"width": 1280, "height": 900, "deviceScaleFactor": 1, "mobile": False})
            cdp.call("Emulation.setEmulatedMedia", {"features": [{"name": "prefers-reduced-motion", "value": "reduce"}]})

            cdp.navigate(server.base + "/index.html")
            cdp.screenshot(os.path.join(evidence_dir, "01-login.png"))

            cdp.evaluate("localStorage.setItem('hust-campus-ai-demo-token-v2', %s)" % json.dumps(student["token"]))
            cdp.navigate(server.base + "/index.html")
            time.sleep(0.5)
            cdp.evaluate("document.getElementById('onboardingClose').click()")
            time.sleep(1.0)
            cdp.screenshot(os.path.join(evidence_dir, "02-student-home.png"))

            cdp.evaluate("document.getElementById('guideNav').click()")
            time.sleep(0.8)
            cdp.screenshot(os.path.join(evidence_dir, "03-guide-step1.png"))
            cdp.evaluate("document.querySelector('#onboardingActions .primary').click()")
            time.sleep(0.8)
            cdp.screenshot(os.path.join(evidence_dir, "04-guide-step2.png"))
            cdp.evaluate("document.querySelector('input[name=product][value=demo]').click(); document.querySelector('#onboardingActions .primary').click()")
            time.sleep(0.8)
            cdp.evaluate("""(() => { const t=document.getElementById('practiceTitle'); t.value='本周校园活动'; t.dispatchEvent(new Event('input')); document.querySelectorAll('#practiceFilters .chip')[1].click(); document.querySelector('#practiceEvents button').click(); })()""")
            time.sleep(0.4)
            cdp.screenshot(os.path.join(evidence_dir, "05-guide-step3.png"))
            cdp.evaluate("document.querySelector('#onboardingActions .primary').click()")
            time.sleep(0.8)
            cdp.screenshot(os.path.join(evidence_dir, "06-guide-step4.png"))
            cdp.evaluate("document.querySelector('input[name=destination][value=gallery]').click()")

            cdp.evaluate("localStorage.setItem('hust-campus-ai-demo-token-v2', %s)" % json.dumps(teacher["token"]))
            cdp.navigate(server.base + "/teacher.html")
            time.sleep(1.5)
            checks = {
                "teacherMembers": cdp.value("document.querySelectorAll('#memberTable tbody tr').length"),
                "teacherWorks": cdp.value("document.querySelectorAll('#pendingWorks .work-card').length"),
                "teacherErrorHidden": cdp.value("document.getElementById('teacherError').classList.contains('hidden')"),
            }
            cdp.screenshot(os.path.join(evidence_dir, "07-teacher.png"))
            cdp.evaluate("window.scrollTo(0, document.getElementById('memberTable').getBoundingClientRect().top + window.scrollY - 90)")
            time.sleep(0.3)
            cdp.screenshot(os.path.join(evidence_dir, "07-teacher-members.png"))

            cdp.navigate(server.base + "/admin.html")
            cdp.evaluate("sessionStorage.setItem('hust-campus-ai-admin-token-v1', %s); location.reload()" % json.dumps(admin))
            time.sleep(2.5)
            checks.update({
                "adminOrgs": cdp.value("document.querySelectorAll('#adminOrgTree .tree-branch').length"),
                "adminMembers": cdp.value("document.querySelectorAll('#adminMemberTable tbody tr').length"),
                "adminRelay": cdp.value("document.querySelectorAll('#relayTable tbody tr').length"),
            })
            cdp.screenshot(os.path.join(evidence_dir, "08-admin.png"))
            cdp.evaluate("window.scrollTo(0, document.getElementById('adminMemberTable').getBoundingClientRect().top + window.scrollY - 90)")
            time.sleep(0.3)
            cdp.screenshot(os.path.join(evidence_dir, "08-admin-members.png"))
            assert checks["teacherMembers"] >= 2, checks
            assert checks["teacherWorks"] >= 1, checks
            assert checks["teacherErrorHidden"] is True, checks
            assert checks["adminOrgs"] >= 1, checks
            assert checks["adminMembers"] >= 2, checks
            assert checks["adminRelay"] >= 1, checks
            cdp.ws.close()
            print("checks:", json.dumps(checks, ensure_ascii=False))
        finally:
            chrome.terminate()
            try:
                chrome.wait(timeout=5)
            except subprocess.TimeoutExpired:
                chrome.kill()
            shutil.rmtree(profile, ignore_errors=True)
    print("screenshots:", sorted(os.listdir(evidence_dir)))


if __name__ == "__main__":
    target_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "evidence")
    run(os.path.abspath(target_dir))
