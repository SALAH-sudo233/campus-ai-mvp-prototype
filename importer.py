"""UTF-8 CSV 名单解析与校验。解析结果只用于预览，确认阶段再写成员表。"""
import csv
import hashlib
import io
import re

MAX_BYTES = 10 * 1024 * 1024
MAX_ROWS = 10000
MAX_FIELD = 256
MAX_LINE = 4096
SCHEMA_VERSION = 1
COLUMNS = ["external_ref", "student_id", "email", "name", "role", "status"]
ROLE_ALIASES = {"org_admin": "org_admin", "org_member": "org_member", "教师": "org_admin", "学生": "org_member"}
STATUSES = {"active", "disabled"}
FORMULA_PREFIXES = ("=", "+", "-", "@")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,20}$")
STUDENT_RE = re.compile(r"^[A-Za-z0-9_-]{2,32}$")


class ImportError_(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def digest(data):
    return hashlib.sha256(data).hexdigest()


def mask_email(value):
    if not value or "@" not in value:
        return value or ""
    local, domain = value.split("@", 1)
    return (local[:1] or "*") + "***@***." + (domain.rsplit(".", 1)[-1] if "." in domain else "***")


def mask_student(value):
    value = str(value or "")
    if len(value) <= 4:
        return "***"
    return value[:2] + "***" + value[-2:]


def masked_fields(fields):
    return {
        "external_ref": fields.get("external_ref", "")[:32],
        "student_id": mask_student(fields.get("student_id", "")),
        "email": mask_email(fields.get("email", "")),
        "name": fields.get("name", ""),
        "role": fields.get("role", ""),
        "status": fields.get("status", ""),
    }


def escape_cell(value):
    text = str(value or "")
    if text.startswith(FORMULA_PREFIXES):
        return "'" + text
    return text


def _reason(code, message):
    return {"result": "rejected", "reason_code": code, "message": message}


def parse(data):
    if len(data) > MAX_BYTES:
        raise ImportError_("file_too_large", "文件超过 10 MB 上限", 413)
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ImportError_("invalid_encoding", "文件必须是 UTF-8 编码")
    if "\x00" in text:
        raise ImportError_("invalid_character", "文件包含 NUL 字符")
    lines = text.splitlines(keepends=True)
    if len(lines) > MAX_ROWS + 1:
        raise ImportError_("row_limit_exceeded", "文件最多 10,000 行")
    for index, line in enumerate(lines, 1):
        if len(line.encode("utf-8")) > MAX_LINE:
            raise ImportError_("line_too_long", "第 %d 行超过 4 KB" % index)
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        raw_rows = list(reader)
    except csv.Error as exc:
        raise ImportError_("invalid_quote", "CSV 引号格式不正确：%s" % str(exc)[:80])
    if not raw_rows:
        raise ImportError_("empty_file", "文件为空")
    header = [cell.strip() for cell in raw_rows[0]]
    if header != COLUMNS:
        raise ImportError_("invalid_header", "表头必须依次为：" + ",".join(COLUMNS))
    rows = []
    seen_refs = {}
    seen_identity = {}
    for row_no, raw in enumerate(raw_rows[1:], 2):
        if not raw or all(not cell.strip() for cell in raw):
            continue
        if len(raw) != len(COLUMNS):
            rows.append({"row_no": row_no, "row_key": None, "fields": {}, "masked": {},
                         **_reason("column_count", "列数必须为 6 列")})
            continue
        fields = {COLUMNS[i]: (raw[i] or "").strip() for i in range(len(COLUMNS))}
        if any(len(value) > MAX_FIELD for value in fields.values()):
            rows.append({"row_no": row_no, "row_key": fields.get("external_ref"), "fields": fields,
                         "masked": masked_fields(fields), **_reason("field_too_long", "单字段不能超过 256 字符")})
            continue
        if any(CONTROL_RE.search(value) for value in fields.values()):
            rows.append({"row_no": row_no, "row_key": fields.get("external_ref"), "fields": fields,
                         "masked": masked_fields(fields), **_reason("control_character", "字段包含控制字符")})
            continue
        formula_field = next((name for name, value in fields.items() if value.startswith(FORMULA_PREFIXES)), None)
        if formula_field:
            rows.append({"row_no": row_no, "row_key": fields.get("external_ref"), "fields": fields,
                         "masked": masked_fields(fields), **_reason("formula_prefix", "字段不能以公式前缀开头")})
            continue
        role = ROLE_ALIASES.get(fields["role"])
        status = fields["status"].lower()
        problem = None
        if not fields["external_ref"] or len(fields["external_ref"]) > 128:
            problem = ("invalid_external_ref", "external_ref 必填且不能超过 128 字符")
        elif not fields["name"]:
            problem = ("missing_name", "姓名必填")
        elif fields["student_id"] and not STUDENT_RE.match(fields["student_id"]):
            problem = ("invalid_student_id", "学号格式不正确")
        elif fields["email"] and not EMAIL_RE.match(fields["email"]):
            problem = ("invalid_email", "邮箱格式不正确")
        elif not fields["student_id"] and not fields["email"]:
            problem = ("missing_identity", "学号和邮箱至少填一个")
        elif role is None:
            problem = ("invalid_role", "role 只能是 org_admin 或 org_member，也可填教师或学生")
        elif status not in STATUSES:
            problem = ("invalid_status", "status 只能是 active 或 disabled")
        if problem:
            rows.append({"row_no": row_no, "row_key": fields.get("external_ref"), "fields": fields,
                         "masked": masked_fields(fields), **_reason(problem[0], problem[1])})
            continue
        fields["role"] = role
        fields["status"] = status
        fields["email"] = fields["email"].lower()
        identity_keys = []
        if fields["student_id"]:
            identity_keys.append("student:" + fields["student_id"])
        if fields["email"]:
            identity_keys.append("email:" + fields["email"])
        duplicate = None
        if fields["external_ref"] in seen_refs:
            duplicate = ("duplicate_in_file", "文件内 external_ref 重复，首次出现位于第 %d 行" % seen_refs[fields["external_ref"]])
        else:
            for key in identity_keys:
                if key in seen_identity:
                    duplicate = ("duplicate_identity", "文件内身份重复，首次出现位于第 %d 行" % seen_identity[key])
                    break
        if duplicate:
            rows.append({"row_no": row_no, "row_key": fields["external_ref"], "fields": fields,
                         "masked": masked_fields(fields), **_reason(duplicate[0], duplicate[1])})
            continue
        seen_refs[fields["external_ref"]] = row_no
        for key in identity_keys:
            seen_identity[key] = row_no
        rows.append({"row_no": row_no, "row_key": fields["external_ref"], "fields": fields,
                     "masked": masked_fields(fields), "result": "accepted", "reason_code": None, "message": None})
    return rows


def template_csv():
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(COLUMNS)
    writer.writerow(["roster-a-001", "2024001001", "2024001001@mail.hust.edu.cn", "示例学生", "org_member", "active"])
    writer.writerow(["teacher-a-001", "2024001100", "2024001100@mail.hust.edu.cn", "示例教师", "org_admin", "active"])
    return buffer.getvalue().encode("utf-8")


def errors_csv(rows):
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["row_no", "reason_code", "message", "external_ref", "student_id", "email", "name", "role", "status"])
    for row in rows:
        fields = row.get("masked") or masked_fields(row.get("fields") or {})
        writer.writerow([row["row_no"], row.get("reason_code") or "", row.get("message") or "",
                         escape_cell(fields.get("external_ref")), escape_cell(fields.get("student_id")),
                         escape_cell(fields.get("email")), escape_cell(fields.get("name")),
                         escape_cell(fields.get("role")), escape_cell(fields.get("status"))])
    return buffer.getvalue().encode("utf-8")
