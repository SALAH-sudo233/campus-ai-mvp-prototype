"""登录身份口径：邮箱一律去首尾空白并转小写，学号只去空白、保持原样。

所有入口（注册、登录、导入预览与确认、迁移、中转站台账）都复用这里的函数，
避免各处各写一份 lower() 导致口径漂移。
"""
import hashlib


def normalize_identity(mode, identity):
    """返回业务使用的规范身份；mode 为 email 时转小写，其余模式保持原样。"""
    value = str(identity or "").strip()
    if mode == "email":
        return value.lower()
    return value


def owner_ref_of(mode, identity):
    """账户覆盖引用由规范身份派生，大小写不同的写法必须落到同一个账户。"""
    value = normalize_identity(mode, identity)
    return "stu_" + hashlib.sha256(("%s:%s" % (mode, value)).encode("utf-8")).hexdigest()[:16]
