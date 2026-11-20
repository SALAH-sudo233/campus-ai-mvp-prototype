import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
def free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class DemoServer:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="campus-test-")
        self.port = free_port()
        self.base = "http://127.0.0.1:%d" % self.port
        self.proc = None
        self.admin_user = "test-admin"
        self.admin_secret = "test-" + secrets.token_urlsafe(24)

    @property
    def db_path(self):
        return os.path.join(self.dir, "backend.sqlite")

    @property
    def relay_path(self):
        return os.path.join(self.dir, "relay.sqlite")

    def __enter__(self):
        env = os.environ.copy()
        env.update({
            "DEMO_DB": self.db_path,
            "DEMO_RELAY_DB": self.relay_path,
            "ADMIN_USERNAME": self.admin_user,
            "ADMIN_PASSWORD": self.admin_secret,
            "DEMO_ALLOW_RESET": "1",
            "HOST": "127.0.0.1",
            "PORT": str(self.port),
        })
        self.proc = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=env,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                data = self.get("/api/health")
                if data[0] == 200:
                    return self
            except OSError:
                pass
            time.sleep(0.1)
        output = self.proc.stdout.read().decode("utf-8", "replace") if self.proc.stdout else ""
        raise RuntimeError("server did not start: " + output)

    def __exit__(self, exc_type, exc, tb):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        if self.proc and self.proc.stdout:
            self.proc.stdout.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def request(self, method, path, body=None, token=None, raw=None, content_type=None):
        data = None
        headers = {}
        if raw is not None:
            data = raw
            headers["Content-Type"] = content_type or "text/csv; charset=utf-8"
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                content = resp.read()
                code = resp.status
                ctype = resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as exc:
            content = exc.read()
            code = exc.code
            ctype = exc.headers.get("Content-Type", "")
        if "application/json" in ctype:
            try:
                payload = json.loads(content.decode("utf-8"))
            except ValueError:
                payload = {}
        else:
            payload = content
        return code, payload

    def get(self, path, token=None):
        return self.request("GET", path, token=token)

    def post(self, path, body=None, token=None):
        return self.request("POST", path, body=body if body is not None else {}, token=token)

    def patch(self, path, body, token=None):
        return self.request("PATCH", path, body=body, token=token)

    def admin_token(self):
        code, data = self.post("/api/admin/login", {"username": self.admin_user, "password": self.admin_secret})
        assert code == 200, data
        return data["token"]


def register(server, identity, password="student-pass-123", mode="student", org_id=None, request_id=None):
    body = {"mode": mode, "identity": identity, "password": password,
            "requestId": request_id or "reg-" + identity}
    if org_id:
        body["orgId"] = org_id
    return server.post("/api/register", body)


def login(server, identity, password="student-pass-123", mode="student", org_id=None):
    body = {"mode": mode, "identity": identity, "password": password, "requestId": "login-" + identity}
    if org_id:
        body["orgId"] = org_id
    return server.post("/api/login", body)
