"""测试用 New-API 协议桩。只实现 token 管理与故障注入，不记录或输出凭据。"""
import json
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeNewApi:
    def __init__(self):
        self.token = "test-" + secrets.token_urlsafe(18)
        self.lock = threading.Lock()
        self.tokens = {}
        self.next_id = 1
        self.fail_mode = None
        self.timeout_sleep = 1.1
        self.create_count = 0
        self.list_count = 0
        self.key_fetch_count = 0
        self.delete_count = 0
        self.last_create_payload = None
        self.last_key_method = None
        self.disable_get_key = False
        self.server = None
        self.thread = None

    @property
    def base_url(self):
        return "http://127.0.0.1:%d" % self.server.server_address[1]

    def __enter__(self):
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _authorized(self):
                return self.headers.get("Authorization") == "Bearer " + fake.token

            def _json(self, code, payload):
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

            def _body(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    return json.loads(self.rfile.read(length) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    return {}

            def _fail(self, mode):
                if mode == "timeout":
                    time.sleep(fake.timeout_sleep)
                    return True
                if mode == "http_500":
                    self._json(500, {"success": False, "message": "upstream error"})
                    return True
                if mode == "auth_failed":
                    self._json(401, {"success": False, "message": "unauthorized"})
                    return True
                if mode == "rate_limited":
                    self._json(429, {"success": False, "message": "rate limited"})
                    return True
                if mode == "quota":
                    self._json(200, {"success": False, "message": "insufficient quota"})
                    return True
                if mode == "token_limit":
                    self._json(200, {"success": False, "message": "maximum number of tokens reached"})
                    return True
                if mode == "phantom_success":
                    self._json(200, {"success": True, "message": "", "data": None})
                    return True
                return False

            def do_GET(self):
                if not self._authorized():
                    return self._json(401, {"success": False, "message": "unauthorized"})
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path in ("/api/token/", "/api/token"):
                    with fake.lock:
                        fake.list_count += 1
                        values = list(fake.tokens.values())
                    query = urllib.parse.parse_qs(parsed.query)
                    page = max(int((query.get("p") or ["0"])[0]), 0)
                    size = min(max(int((query.get("page_size") or query.get("size") or ["10"])[0]), 1), 100)
                    begin = page * size
                    batch = [self._masked(item) for item in values[begin:begin + size]]
                    return self._json(200, {"success": True, "message": "",
                                            "data": {"page": page, "page_size": size, "total": len(values),
                                                     "items": batch}})
                if parsed.path == "/api/token/search":
                    query = urllib.parse.parse_qs(parsed.query)
                    keyword = (query.get("keyword") or [""])[0]
                    with fake.lock:
                        values = [item for item in fake.tokens.values() if keyword in item["name"]]
                    return self._json(200, {"success": True, "message": "",
                                            "data": {"items": [self._masked(item) for item in values],
                                                     "total": len(values)}})
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "token"] and parts[3] == "key":
                    if fake.disable_get_key:
                        return self._json(405, {"success": False, "message": "method not allowed"})
                    try:
                        token_id = int(parts[2])
                    except ValueError:
                        return self._json(400, {"success": False, "message": "bad token id"})
                    with fake.lock:
                        item = fake.tokens.get(token_id)
                        if item is None:
                            return self._json(404, {"success": False, "message": "not found"})
                        fake.key_fetch_count += 1
                        fake.last_key_method = "GET"
                        key = item["key"]
                    return self._json(200, {"success": True, "message": "", "data": {"key": key}})
                return self._json(404, {"success": False, "message": "not found"})

            def do_POST(self):
                if not self._authorized():
                    return self._json(401, {"success": False, "message": "unauthorized"})
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path in ("/api/token/", "/api/token"):
                    mode = fake.fail_mode
                    if self._fail(mode):
                        return None
                    data = self._body()
                    name = str(data.get("name") or "")
                    with fake.lock:
                        fake.create_count += 1
                        fake.last_create_payload = dict(data)
                        if mode == "create_conflict_once":
                            fake.fail_mode = None
                            item = fake._add(name, int(data.get("remain_quota") or 0),
                                             bool(data.get("unlimited_quota")))
                            return self._json(409, {"success": False, "message": "duplicate token name",
                                                    "data": {"id": item["id"]}})
                        if mode == "drop_create_response_once":
                            fake.fail_mode = None
                            fake._add(name, int(data.get("remain_quota") or 0),
                                      bool(data.get("unlimited_quota")))
                            return self._json(500, {"success": False, "message": "upstream error"})
                        item = fake._add(name, int(data.get("remain_quota") or 0),
                                         bool(data.get("unlimited_quota")))
                    return self._json(200, {"success": True, "message": "", "data": {"id": item["id"]}})
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 4 and parts[:2] == ["api", "token"] and parts[3] == "key":
                    try:
                        token_id = int(parts[2])
                    except ValueError:
                        return self._json(400, {"success": False, "message": "bad token id"})
                    with fake.lock:
                        item = fake.tokens.get(token_id)
                        if item is None:
                            return self._json(404, {"success": False, "message": "not found"})
                        fake.key_fetch_count += 1
                        fake.last_key_method = "POST"
                        key = item["key"]
                    return self._json(200, {"success": True, "message": "", "data": {"key": key}})
                return self._json(404, {"success": False, "message": "not found"})

            def do_DELETE(self):
                if not self._authorized():
                    return self._json(401, {"success": False, "message": "unauthorized"})
                parsed = urllib.parse.urlparse(self.path)
                parts = parsed.path.strip("/").split("/")
                if len(parts) == 3 and parts[:2] == ["api", "token"]:
                    try:
                        token_id = int(parts[2])
                    except ValueError:
                        return self._json(400, {"success": False, "message": "bad token id"})
                    with fake.lock:
                        if fake.fail_mode == "delete_fail":
                            return self._json(500, {"success": False, "message": "delete failed"})
                        item = fake.tokens.pop(token_id, None)
                        if item is None:
                            return self._json(404, {"success": False, "message": "not found"})
                        fake.delete_count += 1
                    return self._json(200, {"success": True, "message": ""})
                return self._json(404, {"success": False, "message": "not found"})

            def _masked(self, item):
                key = item["key"]
                masked = "********" if len(key) <= 8 else key[:4] + "**********" + key[-4:]
                return {"id": item["id"], "name": item["name"], "status": 1, "key": masked,
                        "remain_quota": item["remain_quota"], "used_quota": item["used_quota"],
                        "unlimited_quota": item["unlimited_quota"], "expired_time": -1,
                        "created_time": item["created_time"]}

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def _add(self, name, quota=0, unlimited=False):
        item = {"id": self.next_id, "name": name, "key": "sk-test-" + secrets.token_urlsafe(24),
                "remain_quota": int(quota), "used_quota": 0, "unlimited_quota": bool(unlimited),
                "created_time": int(time.time())}
        self.next_id += 1
        self.tokens[item["id"]] = item
        return item

    def add_token(self, name, quota=0, unlimited=False):
        with self.lock:
            return dict(self._add(name, quota, unlimited))

    def token_by_name(self, name):
        with self.lock:
            for item in self.tokens.values():
                if item["name"] == name:
                    return dict(item)
        return None

    def set_quota(self, name, remaining=None, used=None):
        with self.lock:
            for item in self.tokens.values():
                if item["name"] == name:
                    if remaining is not None:
                        item["remain_quota"] = int(remaining)
                    if used is not None:
                        item["used_quota"] = int(used)
                    return

    def __exit__(self, exc_type, exc, tb):
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=5)
