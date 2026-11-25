"""作品状态机、软件交付物托管/打包与组织内展示。"""
import io
import json
import os
import posixpath
import re
import secrets
import shutil
import time
import zipfile

import rewards_service

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024
MAX_PREVIEW_IMAGE_BYTES = 8 * 1024 * 1024
MAX_PREVIEW_IMAGES = 4
MAX_ZIP_FILES = 200
MAX_ZIP_UNCOMPRESSED = 30 * 1024 * 1024

SOFTWARE_TYPES = {
    "web": {"label": "Web 应用", "default_platform": "浏览器", "exts": (".html", ".htm", ".zip")},
    "app": {"label": "App", "default_platform": "Android", "exts": (".apk", ".ipa", ".hap", ".aab", ".zip")},
    "pc": {"label": "PC 软件", "default_platform": "macOS / Windows", "exts": (".dmg", ".exe", ".msi", ".pkg", ".appimage", ".deb", ".zip", ".tar.gz", ".tgz")},
}

SEED_UPVOTES = {
    "demo-campus-events": 18,
    "demo-focus-timer": 14,
    "demo-homework-list": 11,
    "demo-club-poster": 8,
}

SEED_COMMENTS = [
    ("demo-campus-events", "平台演示", "org_member", "把社团招新、技术沙龙和运动活动收在一个网页里，筛选和收藏很顺手。"),
    ("demo-campus-events", "教学助教", "org_admin", "作为 Web 应用直接托管上线很方便，同学点开链接就能体验。"),
    ("demo-focus-timer", "平台演示", "org_member", "自习室刷题时直接开网页分段计时挺实用，界面也很干净。"),
]

DEMO_CAMPUS_EVENTS_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>校园活动清单 · Web 应用示例</title>
<style>
body{margin:0;font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f5f7fb;color:#17233c}
.wrap{max-width:760px;margin:0 auto;padding:32px 20px}.top{display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:20px}
h1{margin:0;font-size:24px}.tag{background:#eef1ff;color:#4968ed;padding:4px 10px;border-radius:20px;font-size:12px;font-weight:700}
.bar{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:18px}.chip{border:1px solid #d8deeb;background:#fff;padding:7px 14px;border-radius:20px;cursor:pointer;font:inherit;color:#51607d}
.chip.active{background:#4968ed;border-color:#4968ed;color:#fff;font-weight:700}
.list{display:flex;flex-direction:column;gap:12px}.card{background:#fff;border:1px solid #e5eaf2;border-radius:14px;padding:16px 18px;display:flex;justify-content:space-between;align-items:center;gap:14px}
.card b{display:block;font-size:16px}.card small{color:#71809b}.btn{border:1px solid #d8deeb;background:#fff;border-radius:9px;padding:7px 13px;cursor:pointer;font:inherit;color:#17233c}
.btn.saved{background:#4968ed;border-color:#4968ed;color:#fff;font-weight:700}
</style></head><body><div class="wrap">
<div class="top"><div><h1>校园活动清单</h1><div style="color:#71809b;font-size:13px">Web 应用托管示例 · 筛选并收藏本周感兴趣的校园活动</div></div><span class="tag" id="favCount">已收藏 0 项</span></div>
<div class="bar" id="cats"></div><div class="list" id="list"></div>
</div><script>
const ITEMS=[
  {id:1,title:"软件学院 AI 编程沙龙",cat:"学习",time:"周二 19:00 · 东九楼 B201"},
  {id:2,title:"百团大战社团招新",cat:"社团",time:"周三 16:30 · 韵苑操场"},
  {id:3,title:"校园荧光夜跑 5km",cat:"运动",time:"周四 20:00 · 中心操场"},
  {id:4,title:"开源 Agent 实战分享会",cat:"学习",time:"周五 15:00 · 南一楼 312"},
  {id:5,title:"草地独立音乐节",cat:"社团",time:"周六 19:30 · 醉晚亭草坪"},
  {id:6,title:"周末羽毛球约球局",cat:"运动",time:"周日 14:30 · 光谷体育馆"}
];
let cat="全部",fav=new Set();
function render(){
  document.getElementById("favCount").textContent="已收藏 "+fav.size+" 项";
  const cats=["全部","学习","社团","运动"];
  document.getElementById("cats").innerHTML=cats.map(c=>'<button class="chip '+(c===cat?'active':'')+'" data-cat="'+c+'">'+c+'</button>').join("");
  document.querySelectorAll("#cats .chip").forEach(b=>b.onclick=()=>setCat(b.dataset.cat));
  const rows=ITEMS.filter(x=>cat==="全部"||x.cat===cat);
  document.getElementById("list").innerHTML=rows.map(x=>'<div class="card"><div><b>'+x.title+'</b><small>'+x.cat+' · '+x.time+'</small></div><button class="btn '+(fav.has(x.id)?'saved':'')+'" onclick="toggle('+x.id+')">'+(fav.has(x.id)?'已收藏 ★':'收藏')+'</button></div>').join("");
}
window.setCat=c=>{cat=c;render();};
window.toggle=id=>{fav.has(id)?fav.delete(id):fav.add(id);render();};
render();
</script></body></html>"""

DEMO_FOCUS_TIMER_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="UTF-8"/><meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>自习番茄钟 · Web 应用示例</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;background:#f5f7fb;color:#17233c}
.box{background:#fff;border:1px solid #e5eaf2;border-radius:20px;padding:34px 30px;width:min(92vw,420px);text-align:center;box-shadow:0 18px 40px #25345d0f}
.modes{display:flex;gap:6px;justify-content:center;margin-bottom:22px}.m{border:1px solid #e5eaf2;background:#f8faff;padding:7px 14px;border-radius:20px;cursor:pointer;font:inherit;color:#51607d}
.m.active{background:#17a673;border-color:#17a673;color:#fff;font-weight:700}
.clock{font-size:64px;font-weight:800;letter-spacing:-2px;margin:10px 0 22px;font-variant-numeric:tabular-nums;color:#17233c}
.actions{display:flex;gap:10px;justify-content:center}.primary{border:0;background:#17a673;color:#fff;padding:11px 26px;border-radius:10px;font:inherit;font-weight:700;cursor:pointer}
.ghost{border:1px solid #d8deeb;background:#fff;color:#51607d;padding:11px 18px;border-radius:10px;font:inherit;cursor:pointer}
</style></head><body><div class="box">
<div style="color:#17a673;font-size:12px;font-weight:800;letter-spacing:1.2px">WEB APP DEMO</div>
<h2 style="margin:4px 0 18px">自习番茄钟</h2>
<div class="modes"><button class="m active" onclick="setMode(25,this)">专注 25 分钟</button><button class="m" onclick="setMode(5,this)">短休 5 分钟</button><button class="m" onclick="setMode(15,this)">长休 15 分钟</button></div>
<div class="clock" id="clock">25:00</div>
<div class="actions"><button class="primary" id="toggleBtn" onclick="toggle()">开始专注</button><button class="ghost" onclick="reset()">重置</button></div>
<p style="color:#71809b;font-size:12px;margin:18px 0 0" id="done">今日已完成 0 个番茄</p>
</div><script>
let baseSec=25*60,left=baseSec,timer=null,done=0;
function draw(){const m=String(Math.floor(left/60)).padStart(2,"0"),s=String(left%60).padStart(2,"0");document.getElementById("clock").textContent=m+":"+s;document.getElementById("toggleBtn").textContent=timer?"暂停":"开始专注";}
window.setMode=(min,btn)=>{clearInterval(timer);timer=null;baseSec=min*60;left=baseSec;document.querySelectorAll(".m").forEach(n=>n.classList.remove("active"));btn.classList.add("active");draw();};
window.toggle=()=>{if(timer){clearInterval(timer);timer=null;draw();return;}timer=setInterval(()=>{left--;if(left<=0){clearInterval(timer);timer=null;done++;document.getElementById("done").textContent="今日已完成 "+done+" 个番茄";left=baseSec;}draw();},1000);draw();};
window.reset=()=>{clearInterval(timer);timer=null;left=baseSec;draw();};
draw();
</script></body></html>"""


class WorkError(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _safe_filename(name, fallback="package.zip"):
    base = os.path.basename(str(name or "").strip().replace("\\", "/"))
    base = re.sub(r"[^a-zA-Z0-9._\-\u4e00-\u9fff]+", "_", base).strip("._")
    return base[:100] or fallback


def _match_allowed_ext(filename, exts):
    lower = filename.lower()
    return any(lower.endswith(ext) for ext in exts)


def _normalize_software_type(value, category_hint=None):
    raw = str(value or "").strip().lower()
    if raw in SOFTWARE_TYPES:
        return raw
    mapping = {"web 应用": "web", "web": "web", "网站": "web", "app": "app", "移动端": "app", "pc 软件": "pc", "pc": "pc", "桌面软件": "pc"}
    if raw in mapping:
        return mapping[raw]
    hint = str(category_hint or "").strip().lower()
    return mapping.get(hint, "web")


def _validate_external_url(url):
    text = str(url or "").strip()
    if not text:
        return ""
    if len(text) > 500 or not (text.startswith("http://") or text.startswith("https://")):
        raise WorkError(400, "invalid_url", "链接必须以 http:// 或 https:// 开头")
    return text


def ensure_social_tables(conn, data_dir=None):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_upvotes ("
        "work_id TEXT NOT NULL, user_id TEXT NOT NULL, created_at REAL NOT NULL, "
        "PRIMARY KEY (work_id, user_id))"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_comments ("
        "id TEXT PRIMARY KEY, work_id TEXT NOT NULL, org_id TEXT NOT NULL, user_id TEXT NOT NULL, "
        "author_name TEXT NOT NULL, author_role TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_work_comments_work ON work_comments (work_id, created_at)")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS work_artifacts ("
        "id TEXT PRIMARY KEY, owner_user_id TEXT, org_id TEXT, software_type TEXT NOT NULL, "
        "file_name TEXT NOT NULL, file_size INTEGER NOT NULL, storage_path TEXT NOT NULL, "
        "entry_path TEXT, created_at REAL NOT NULL)"
    )
    now = time.time()
    if conn.execute("SELECT COUNT(*) FROM work_upvotes").fetchone()[0] == 0:
        existing_works = {r["work_id"]: r["org_id"] for r in conn.execute(
            "SELECT work_id, org_id FROM works WHERE source_kind = 'seed_demo'").fetchall()}
        for work_id, count in SEED_UPVOTES.items():
            if work_id in existing_works:
                for idx in range(count):
                    conn.execute(
                        "INSERT OR IGNORE INTO work_upvotes (work_id, user_id, created_at) VALUES (?, ?, ?)",
                        (work_id, f"seed_voter_{idx + 1}", now - (count - idx) * 600),
                    )
        for idx, (work_id, author_name, author_role, body) in enumerate(SEED_COMMENTS, 1):
            org_id = existing_works.get(work_id)
            if org_id:
                conn.execute(
                    "INSERT OR IGNORE INTO work_comments (id, work_id, org_id, user_id, author_name, author_role, body, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (f"cmt_seed_{idx}", work_id, org_id, f"seed_author_{idx}", author_name, author_role, body, now - idx * 3600),
                )
    if data_dir:
        ensure_showcase_artifacts(conn, data_dir)


def _write_demo_zip(path, readme_text, extra_files=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("README.txt", readme_text)
            for fname, content in (extra_files or {}).items():
                zf.writestr(fname, content)
    return os.path.getsize(path)


def ensure_showcase_artifacts(conn, data_dir):
    """为 4 个内置示例作品生成真实的 Web 托管站点和 App/PC 安装包示例。"""
    hosted_root = os.path.join(data_dir, "hosted")
    pkg_root = os.path.join(data_dir, "packages")
    media_root = os.path.join(data_dir, "media")
    os.makedirs(hosted_root, exist_ok=True)
    os.makedirs(pkg_root, exist_ok=True)
    os.makedirs(media_root, exist_ok=True)

    demo_src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "demo-media")
    demo_images = {
        "demo-campus-events": ["demo_campus_events_1.jpg", "demo_campus_events_2.jpg"],
        "demo-focus-timer": ["demo_focus_timer_1.jpg", "demo_focus_timer_2.jpg"],
        "demo-homework-list": ["demo_homework_app_1.jpg", "demo_homework_app_2.jpg"],
        "demo-club-poster": ["demo_club_poster_1.jpg", "demo_club_poster_2.jpg"],
    }
    work_media_urls = {}
    for wid, files in demo_images.items():
        urls = []
        for fname in files:
            src = os.path.join(demo_src_dir, fname)
            dst = os.path.join(media_root, fname)
            if os.path.isfile(src):
                if not os.path.exists(dst):
                    shutil.copy2(src, dst)
                urls.append("media/" + fname)
            elif os.path.isfile(dst):
                urls.append("media/" + fname)
        work_media_urls[wid] = urls

    # 1 & 2: Two hosted Web apps
    web_demos = [
        ("demo-campus-events", "校园活动清单", "在线筛选、收藏本周校园活动，支持直接托管成可访问网站。", "v1.2.0", DEMO_CAMPUS_EVENTS_HTML),
        ("demo-focus-timer", "自习番茄钟", "支持专注 25 分钟与休息切换的轻量番茄钟网页应用。", "v1.1.0", DEMO_FOCUS_TIMER_HTML),
    ]
    for work_id, title, summary, version, html in web_demos:
        d = os.path.join(hosted_root, work_id)
        os.makedirs(d, exist_ok=True)
        index_path = os.path.join(d, "index.html")
        with open(index_path, "w", encoding="utf-8") as fh:
            fh.write(html)
        size = os.path.getsize(index_path)
        conn.execute(
            "INSERT OR REPLACE INTO work_artifacts (id, owner_user_id, org_id, software_type, file_name, file_size, storage_path, entry_path, created_at) "
            "VALUES (?, NULL, NULL, 'web', 'index.html', ?, ?, 'index.html', ?)",
            (work_id, size, d, time.time()),
        )
        target = {
            "softwareType": "web", "version": version, "platform": "浏览器",
            "aiTools": ["Codex", "Cursor"], "hostedUrl": f"hosted/{work_id}/index.html",
            "fileName": "index.html", "fileSize": size,
            "previewImages": work_media_urls.get(work_id, []),
        }
        conn.execute(
            "UPDATE works SET title = ?, summary = ?, category = 'Web 应用', artifact_ref = ?, open_target_json = ? "
            "WHERE work_id = ? AND source_kind = 'seed_demo'",
            (title, summary, work_id, json.dumps(target, ensure_ascii=False), work_id),
        )

    # 3: App package demo
    app_pkg_name = "campus-homework-helper-v1.0.3-android-demo.zip"
    app_pkg_path = os.path.join(pkg_root, "demo-homework-list", app_pkg_name)
    app_size = _write_demo_zip(
        app_pkg_path,
        "小组作业助手 App（演示包 v1.0.3）\n适用平台：Android\n说明：这是平台内置的 App 安装包上传示例，真实学生发布时可直接上传 .apk / .ipa / .hap 包。",
        {"manifest.json": json.dumps({"app": "campus-homework-helper", "platform": "Android", "version": "1.0.3"}, ensure_ascii=False, indent=2)},
    )
    conn.execute(
        "INSERT OR REPLACE INTO work_artifacts (id, owner_user_id, org_id, software_type, file_name, file_size, storage_path, entry_path, created_at) "
        "VALUES ('demo-homework-list', NULL, NULL, 'app', ?, ?, ?, NULL, ?)",
        (app_pkg_name, app_size, app_pkg_path, time.time()),
    )
    app_target = {
        "softwareType": "app", "version": "v1.0.3", "platform": "Android",
        "aiTools": ["欢喜 OS", "Codex"], "fileName": app_pkg_name, "fileSize": app_size,
        "previewImages": work_media_urls.get("demo-homework-list", []),
    }
    conn.execute(
        "UPDATE works SET title = '小组作业助手 App', summary = '管理课程小组任务、截止提醒与分工进度的校园移动端应用。', "
        "category = 'App', artifact_ref = 'demo-homework-list', open_target_json = ? "
        "WHERE work_id = 'demo-homework-list' AND source_kind = 'seed_demo'",
        (json.dumps(app_target, ensure_ascii=False),),
    )

    # 4: PC software package demo
    pc_pkg_name = "club-poster-studio-v0.9.4-desktop-demo.zip"
    pc_pkg_path = os.path.join(pkg_root, "demo-club-poster", pc_pkg_name)
    pc_size = _write_demo_zip(
        pc_pkg_path,
        "社团海报排版工具（PC 软件演示包 v0.9.4）\n适用系统：macOS / Windows\n说明：这是平台内置的 PC 软件安装包上传示例，真实学生发布时可上传 .dmg / .exe / .msi / .zip 包。",
        {"version.txt": "0.9.4\n"},
    )
    conn.execute(
        "INSERT OR REPLACE INTO work_artifacts (id, owner_user_id, org_id, software_type, file_name, file_size, storage_path, entry_path, created_at) "
        "VALUES ('demo-club-poster', NULL, NULL, 'pc', ?, ?, ?, NULL, ?)",
        (pc_pkg_name, pc_size, pc_pkg_path, time.time()),
    )
    pc_target = {
        "softwareType": "pc", "version": "v0.9.4", "platform": "macOS / Windows",
        "aiTools": ["Claude Code"], "fileName": pc_pkg_name, "fileSize": pc_size,
        "previewImages": work_media_urls.get("demo-club-poster", []),
    }
    conn.execute(
        "UPDATE works SET title = '社团海报排版工具', summary = '本地离线编辑社团活动标题、时间和排版并一键导出高清海报。', "
        "category = 'PC 软件', artifact_ref = 'demo-club-poster', open_target_json = ? "
        "WHERE work_id = 'demo-club-poster' AND source_kind = 'seed_demo'",
        (json.dumps(pc_target, ensure_ascii=False),),
    )


def save_artifact(conn, data_dir, user_id, org_id, software_type, filename, payload):
    sw = _normalize_software_type(software_type)
    spec = SOFTWARE_TYPES[sw]
    if not payload:
        raise WorkError(400, "empty_file", "上传文件不能为空")
    if len(payload) > MAX_ARTIFACT_BYTES:
        raise WorkError(413, "file_too_large", "文件不能超过 50 MB")
    safe_name = _safe_filename(filename, "index.html" if sw == "web" else "package.zip")
    if not _match_allowed_ext(safe_name, spec["exts"]):
        raise WorkError(400, "unsupported_type", "不支持的文件格式，允许：" + " / ".join(spec["exts"]))

    art_id = "art_" + secrets.token_hex(8)
    now = time.time()

    if sw == "web":
        dest_dir = os.path.join(data_dir, "hosted", art_id)
        os.makedirs(dest_dir, exist_ok=True)
        entry_path = "index.html"
        if safe_name.lower().endswith((".html", ".htm")):
            with open(os.path.join(dest_dir, "index.html"), "wb") as fh:
                fh.write(payload)
        else:
            try:
                zf = zipfile.ZipFile(io.BytesIO(payload))
            except zipfile.BadZipFile:
                raise WorkError(400, "bad_zip", "ZIP 压缩包格式损坏，无法解析")
            with zf:
                infos = [i for i in zf.infolist() if not i.is_dir()]
                if not infos:
                    raise WorkError(400, "empty_zip", "ZIP 包里没有文件")
                if len(infos) > MAX_ZIP_FILES:
                    raise WorkError(400, "too_many_files", f"ZIP 内文件数不能超过 {MAX_ZIP_FILES} 个")
                total_size = 0
                norm_entries = []
                for info in infos:
                    raw_name = info.filename.replace("\\", "/")
                    if raw_name.startswith("__MACOSX/") or os.path.basename(raw_name).startswith("._"):
                        continue
                    norm = posixpath.normpath(raw_name).lstrip("/")
                    if norm.startswith("..") or "/../" in ("/" + norm) or norm in (".", ""):
                        raise WorkError(400, "unsafe_zip", "ZIP 包含非法路径")
                    # Reject symlinks
                    if (info.external_attr >> 16) & 0o170000 == 0o120000:
                        raise WorkError(400, "unsafe_zip", "ZIP 不允许包含符号链接")
                    total_size += info.file_size
                    if total_size > MAX_ZIP_UNCOMPRESSED:
                        raise WorkError(413, "zip_too_large", "ZIP 解压后不能超过 30 MB")
                    norm_entries.append((info, norm))
                if not norm_entries:
                    raise WorkError(400, "empty_zip", "ZIP 包里没有有效网页文件")
                # Strip single wrapper folder (e.g. dist/index.html -> index.html)
                top_dirs = {n.split("/")[0] for _, n in norm_entries if "/" in n}
                has_root_file = any("/" not in n for _, n in norm_entries)
                strip_prefix = (next(iter(top_dirs)) + "/") if (not has_root_file and len(top_dirs) == 1) else ""
                written = []
                real_root = os.path.realpath(dest_dir)
                for info, norm in norm_entries:
                    rel = norm[len(strip_prefix):] if strip_prefix and norm.startswith(strip_prefix) else norm
                    if not rel or rel.startswith(".."):
                        continue
                    out_path = os.path.realpath(os.path.join(dest_dir, rel))
                    if not (out_path == real_root or out_path.startswith(real_root + os.sep)):
                        raise WorkError(400, "unsafe_zip", "ZIP 包含越界路径")
                    os.makedirs(os.path.dirname(out_path), exist_ok=True)
                    with zf.open(info) as src, open(out_path, "wb") as dst:
                        dst.write(src.read())
                    written.append(rel)
                if "index.html" in written:
                    entry_path = "index.html"
                else:
                    htmls = [w for w in written if w.lower().endswith((".html", ".htm"))]
                    if not htmls:
                        raise WorkError(400, "missing_index", "Web ZIP 包内未找到 index.html 或任何 HTML 页面")
                    entry_path = sorted(htmls, key=lambda s: (s.count("/"), len(s)))[0]
        conn.execute(
            "INSERT INTO work_artifacts (id, owner_user_id, org_id, software_type, file_name, file_size, storage_path, entry_path, created_at) "
            "VALUES (?, ?, ?, 'web', ?, ?, ?, ?, ?)",
            (art_id, user_id, org_id, safe_name, len(payload), dest_dir, entry_path, now),
        )
        return {
            "artifactId": art_id, "softwareType": "web", "fileName": safe_name,
            "fileSize": len(payload), "hostedUrl": f"hosted/{art_id}/{entry_path}",
        }

    # App or PC package
    pkg_dir = os.path.join(data_dir, "packages", art_id)
    os.makedirs(pkg_dir, exist_ok=True)
    out_file = os.path.join(pkg_dir, safe_name)
    with open(out_file, "wb") as fh:
        fh.write(payload)
    conn.execute(
        "INSERT INTO work_artifacts (id, owner_user_id, org_id, software_type, file_name, file_size, storage_path, entry_path, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (art_id, user_id, org_id, sw, safe_name, len(payload), out_file, now),
    )
    return {
        "artifactId": art_id, "softwareType": sw, "fileName": safe_name,
        "fileSize": len(payload),
    }


def _detect_image_ext(payload):
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return ".webp"
    return None


def save_preview_image(data_dir, payload):
    if not payload:
        raise WorkError(400, "empty_image", "图片不能为空")
    if len(payload) > MAX_PREVIEW_IMAGE_BYTES:
        raise WorkError(413, "image_too_large", "单张预览图不能超过 8 MB")
    ext = _detect_image_ext(payload)
    if not ext:
        raise WorkError(400, "invalid_image", "仅支持 PNG、JPG、GIF 或 WebP 格式图片")
    media_dir = os.path.join(data_dir, "media")
    os.makedirs(media_dir, exist_ok=True)
    fname = "img_" + secrets.token_hex(10) + ext
    with open(os.path.join(media_dir, fname), "wb") as fh:
        fh.write(payload)
    return {"url": "media/" + fname, "fileSize": len(payload)}


def _sanitize_preview_images(items):
    if not isinstance(items, list):
        return None
    cleaned = []
    for raw in items:
        s = str(raw or "").strip()
        if re.match(r"^media/(img_[a-f0-9]{20}|demo_[a-z0-9_]{3,40})\.(png|jpg|gif|webp)$", s):
            if s not in cleaned:
                cleaned.append(s)
        if len(cleaned) >= MAX_PREVIEW_IMAGES:
            break
    return cleaned


def get_artifact(conn, artifact_id):
    if not artifact_id:
        return None
    return conn.execute("SELECT * FROM work_artifacts WHERE id = ?", (artifact_id,)).fetchone()


def _card(row):
    try:
        target = json.loads(row["open_target_json"] or "{}")
    except (TypeError, ValueError):
        target = {}
    keys = row.keys()
    sw = _normalize_software_type(target.get("softwareType"), row["category"])
    hosted_url = target.get("hostedUrl") or ""
    external_url = target.get("externalUrl") or ""
    has_package = bool(row["artifact_ref"]) and sw in ("app", "pc")
    return {
        "workId": row["work_id"],
        "title": row["title"],
        "summary": row["summary"],
        "authorDisplayName": row["author_display_name"],
        "category": SOFTWARE_TYPES[sw]["label"],
        "softwareType": sw,
        "version": target.get("version") or "v1.0.0",
        "platform": target.get("platform") or SOFTWARE_TYPES[sw]["default_platform"],
        "aiTools": target.get("aiTools") or [],
        "previewImages": target.get("previewImages") or [],
        "hostedUrl": hosted_url,
        "externalUrl": external_url,
        "downloadUrl": f"api/works/{row['work_id']}/download" if has_package else "",
        "fileName": target.get("fileName") or "",
        "fileSize": int(target.get("fileSize") or 0),
        "artifactId": row["artifact_ref"] or "",
        "coverPath": row["cover_path"],
        "sourceKind": row["source_kind"],
        "visibilityLabel": "演示广场" if row["source_kind"] == "seed_demo" else "本组织",
        "publishedVersion": row["published_version"],
        "updatedAt": row["updated_at"],
        "openTarget": target,
        "featured": bool(row["featured"]) if "featured" in keys else False,
        "status": row["status"],
        "upvoteCount": int(row["upvote_count"]) if "upvote_count" in keys else 0,
        "upvoted": bool(row["upvoted"]) if "upvoted" in keys else False,
        "commentCount": int(row["comment_count"]) if "comment_count" in keys else 0,
        "reward": None,
    }


def attach_rewards(conn, cards):
    if not cards:
        return cards
    rmap = rewards_service.work_reward_map(conn, [c["workId"] for c in cards])
    for card in cards:
        card["reward"] = rmap.get(card["workId"])
    return cards


def gallery(conn, org_id, limit=50, viewer_user_id=None):
    rows = conn.execute(
        "SELECT w.*, "
        "CASE WHEN f.work_id IS NULL THEN 0 ELSE 1 END AS featured, "
        "(SELECT COUNT(*) FROM work_upvotes u WHERE u.work_id = w.work_id) AS upvote_count, "
        "CASE WHEN ? IS NOT NULL AND EXISTS (SELECT 1 FROM work_upvotes u WHERE u.work_id = w.work_id AND u.user_id = ?) THEN 1 ELSE 0 END AS upvoted, "
        "(SELECT COUNT(*) FROM work_comments c WHERE c.work_id = w.work_id) AS comment_count "
        "FROM works w "
        "LEFT JOIN featured_works f ON f.work_id = w.work_id AND f.org_id = w.org_id "
        "WHERE w.org_id = ? AND w.status = 'published' "
        "ORDER BY upvote_count DESC, featured DESC, w.published_at DESC, w.work_id LIMIT ?",
        (viewer_user_id, viewer_user_id, org_id, limit)).fetchall()
    return attach_rewards(conn, [_card(row) for row in rows])


def toggle_upvote(conn, work_id, org_id, user_id):
    row = get(conn, work_id)
    if not row or row["org_id"] != org_id or row["status"] != "published":
        raise WorkError(404, "not_found", "未找到可点赞的已发布作品")
    existing = conn.execute(
        "SELECT 1 FROM work_upvotes WHERE work_id = ? AND user_id = ?", (work_id, user_id)
    ).fetchone()
    if existing:
        conn.execute("DELETE FROM work_upvotes WHERE work_id = ? AND user_id = ?", (work_id, user_id))
        upvoted = False
    else:
        conn.execute(
            "INSERT INTO work_upvotes (work_id, user_id, created_at) VALUES (?, ?, ?)",
            (work_id, user_id, time.time()),
        )
        upvoted = True
    count = conn.execute("SELECT COUNT(*) FROM work_upvotes WHERE work_id = ?", (work_id,)).fetchone()[0]
    return {"workId": work_id, "upvoted": upvoted, "upvoteCount": int(count)}


def list_comments(conn, work_id):
    rows = conn.execute(
        "SELECT id, work_id, author_name, author_role, body, created_at FROM work_comments "
        "WHERE work_id = ? ORDER BY created_at DESC LIMIT 100",
        (work_id,),
    ).fetchall()
    return [{
        "id": r["id"],
        "workId": r["work_id"],
        "authorName": r["author_name"],
        "authorRole": r["author_role"],
        "body": r["body"],
        "createdAt": r["created_at"],
    } for r in rows]


def add_comment(conn, work_id, org_id, user_id, author_name, author_role, body):
    row = get(conn, work_id)
    if not row or row["org_id"] != org_id or row["status"] != "published":
        raise WorkError(404, "not_found", "未找到可评论的已发布作品")
    text = str(body or "").strip()
    if not text or len(text) > 300:
        raise WorkError(400, "invalid_comment", "评论内容必填且不超过 300 字符")
    now = time.time()
    cid = "cmt_" + secrets.token_hex(8)
    conn.execute(
        "INSERT INTO work_comments (id, work_id, org_id, user_id, author_name, author_role, body, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (cid, work_id, org_id, user_id, author_name or "成员", author_role or "org_member", text, now),
    )
    count = conn.execute("SELECT COUNT(*) FROM work_comments WHERE work_id = ?", (work_id,)).fetchone()[0]
    return {
        "comment": {
            "id": cid, "workId": work_id, "authorName": author_name or "成员",
            "authorRole": author_role or "org_member", "body": text, "createdAt": now,
        },
        "commentCount": int(count),
    }


def featured(conn, org_id, limit=12):
    rows = conn.execute(
        "SELECT w.*, 1 AS featured FROM featured_works f JOIN works w ON w.work_id = f.work_id "
        "WHERE f.org_id = ? AND w.org_id = ? AND w.status = 'published' "
        "ORDER BY f.created_at DESC LIMIT ?", (org_id, org_id, limit)).fetchall()
    return attach_rewards(conn, [_card(row) for row in rows])


def mine(conn, user_id):
    rows = conn.execute("SELECT * FROM works WHERE owner_user_id = ? ORDER BY updated_at DESC", (user_id,)).fetchall()
    return attach_rewards(conn, [_card(row) for row in rows])


def get(conn, work_id):
    return conn.execute("SELECT * FROM works WHERE work_id = ?", (work_id,)).fetchone()


def _build_target(conn, user_id, sw, data, existing_target=None, existing_artifact=None):
    target = dict(existing_target or {})
    target["softwareType"] = sw
    version = str(data.get("version") or target.get("version") or "v1.0.0").strip()[:30]
    platform = str(data.get("platform") or target.get("platform") or SOFTWARE_TYPES[sw]["default_platform"]).strip()[:40]
    tools_raw = data.get("aiTools")
    if isinstance(tools_raw, list):
        ai_tools = [str(t).strip()[:24] for t in tools_raw if str(t).strip()][:6]
    else:
        ai_tools = target.get("aiTools") or []
    target["version"] = version
    target["platform"] = platform
    target["aiTools"] = ai_tools
    if "externalUrl" in data:
        target["externalUrl"] = _validate_external_url(data.get("externalUrl"))
    if "previewImages" in data:
        cleaned_imgs = _sanitize_preview_images(data.get("previewImages"))
        if cleaned_imgs is not None:
            target["previewImages"] = cleaned_imgs

    artifact_id = existing_artifact
    if data.get("artifactId"):
        art = get_artifact(conn, str(data.get("artifactId")).strip())
        if not art or (art["owner_user_id"] and art["owner_user_id"] != user_id):
            raise WorkError(400, "invalid_artifact", "上传的产物无效或无权使用")
        if art["software_type"] != sw:
            raise WorkError(400, "artifact_type_mismatch", "上传文件类型与所选软件分类不一致")
        artifact_id = art["id"]
        target["fileName"] = art["file_name"]
        target["fileSize"] = int(art["file_size"])
        if sw == "web" and art["entry_path"]:
            target["hostedUrl"] = f"hosted/{art['id']}/{art['entry_path']}"
        else:
            target.pop("hostedUrl", None)
    return artifact_id, target


def submit(conn, org_id, user_id, author_name, data):
    title = str(data.get("title") or "").strip()
    summary = str(data.get("summary") or "").strip()
    sw = _normalize_software_type(data.get("softwareType"), data.get("category"))
    category = SOFTWARE_TYPES[sw]["label"]
    if not title or len(title) > 60:
        raise WorkError(400, "invalid_title", "作品名称必填且不超过 60 字符")
    if not summary or len(summary) > 160:
        raise WorkError(400, "invalid_summary", "一句话卖点必填且不超过 160 字符")
    artifact_id, target = _build_target(conn, user_id, sw, data)
    now = time.time()
    work_id = "work_" + secrets.token_hex(10)
    # 发布即上榜（本组织可见），教师负责精选与下架兜底
    conn.execute(
        "INSERT INTO works (work_id, org_id, owner_user_id, title, summary, author_display_name, category, "
        "cover_path, source_kind, status, visibility, artifact_ref, open_target_json, published_version, "
        "created_at, updated_at, published_at) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, 'student_demo', 'published', "
        "'organization', ?, ?, 1, ?, ?, ?)",
        (work_id, org_id, user_id, title, summary, author_name or "学生", category,
         artifact_id, json.dumps(target, ensure_ascii=False), now, now, now))
    return get(conn, work_id)


def update(conn, work_id, user_id, data):
    row = get(conn, work_id)
    if not row or row["owner_user_id"] != user_id:
        raise WorkError(403, "forbidden", "无权修改该作品")
    if row["status"] == "removed":
        raise WorkError(409, "invalid_state", "已下架作品无法编辑")
    title = str(data.get("title") or row["title"]).strip()
    summary = str(data.get("summary") or row["summary"]).strip()
    if not title or len(title) > 60:
        raise WorkError(400, "invalid_title", "作品名称必填且不超过 60 字符")
    if not summary or len(summary) > 160:
        raise WorkError(400, "invalid_summary", "一句话卖点必填且不超过 160 字符")
    try:
        old_target = json.loads(row["open_target_json"] or "{}")
    except (TypeError, ValueError):
        old_target = {}
    sw = _normalize_software_type(data.get("softwareType") or old_target.get("softwareType"), row["category"])
    category = SOFTWARE_TYPES[sw]["label"]
    artifact_id, target = _build_target(conn, user_id, sw, data, old_target, row["artifact_ref"])
    now = time.time()
    new_status = "published" if row["status"] in ("withdrawn", "rejected", "pending_review") else row["status"]
    conn.execute(
        "UPDATE works SET title = ?, summary = ?, category = ?, artifact_ref = ?, open_target_json = ?, "
        "status = ?, published_at = COALESCE(published_at, ?), updated_at = ? WHERE work_id = ?",
        (title, summary, category, artifact_id, json.dumps(target, ensure_ascii=False),
         new_status, now, now, work_id))
    return get(conn, work_id)


def withdraw(conn, work_id, user_id):
    row = get(conn, work_id)
    if not row or row["owner_user_id"] != user_id:
        raise WorkError(403, "forbidden", "无权访问该作品")
    if row["status"] not in ("pending_review", "published", "rejected"):
        raise WorkError(409, "invalid_state", "当前状态不能撤回")
    now = time.time()
    conn.execute("UPDATE works SET status = 'withdrawn', withdrawn_at = ?, updated_at = ? WHERE work_id = ?",
                 (now, now, work_id))
    conn.execute("DELETE FROM featured_works WHERE work_id = ?", (work_id,))
    return get(conn, work_id)


def review(conn, work_id, org_id, decision, actor_id):
    row = get(conn, work_id)
    if not row or row["org_id"] != org_id:
        raise WorkError(403, "forbidden", "无权访问该作品")
    if row["status"] != "pending_review":
        raise WorkError(409, "invalid_state", "只有待审核作品可以审核")
    if decision not in ("approve", "reject"):
        raise WorkError(400, "invalid_decision", "审核结果只能是 approve 或 reject")
    now = time.time()
    status = "published" if decision == "approve" else "rejected"
    conn.execute("UPDATE works SET status = ?, updated_at = ?, published_at = ? WHERE work_id = ?",
                 (status, now, now if status == "published" else None, work_id))
    return get(conn, work_id)


def set_featured(conn, work_id, org_id, actor_id, value):
    row = get(conn, work_id)
    if not row or row["org_id"] != org_id:
        raise WorkError(403, "forbidden", "无权访问该作品")
    if value and row["status"] != "published":
        raise WorkError(409, "invalid_state", "只有已发布作品可以精选")
    if value:
        conn.execute("INSERT OR IGNORE INTO featured_works (org_id, work_id, featured_by, created_at) "
                     "VALUES (?, ?, ?, ?)", (org_id, work_id, actor_id, time.time()))
    else:
        conn.execute("DELETE FROM featured_works WHERE org_id = ? AND work_id = ?", (org_id, work_id))
    return get(conn, work_id)


def unpublish(conn, work_id, org_id, removed_by):
    row = get(conn, work_id)
    if not row or (org_id and row["org_id"] != org_id):
        raise WorkError(403, "forbidden", "无权访问该作品")
    if row["status"] not in ("published", "pending_review"):
        raise WorkError(409, "invalid_state", "当前状态不能下架")
    now = time.time()
    conn.execute("UPDATE works SET status = 'removed', removed_by = ?, updated_at = ?, withdrawn_at = ? "
                 "WHERE work_id = ?", (removed_by, now, now, work_id))
    conn.execute("DELETE FROM featured_works WHERE work_id = ?", (work_id,))
    return get(conn, work_id)
