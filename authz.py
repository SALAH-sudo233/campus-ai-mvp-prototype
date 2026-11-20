"""表驱动的服务端权限矩阵。前端隐藏按钮不作为授权依据。"""

PLATFORM_ADMIN = "platform_admin"
ORG_ADMIN = "org_admin"
ORG_MEMBER = "org_member"
ROLES = (PLATFORM_ADMIN, ORG_ADMIN, ORG_MEMBER)

# 准入状态闸：disabled 视同未认证。pending_review 的能力边界待产品拍板，
# 当前保持原有行为，不在这里拦。
BLOCKED_ADMISSION = ("disabled",)


class AuthzError(Exception):
    def __init__(self, status=403, message="无权执行此操作"):
        super().__init__(message)
        self.status = status
        self.message = message


_RULES = {
    PLATFORM_ADMIN: {
        ("organizations", "list"): "all",
        ("organizations", "create"): "all",
        ("organizations", "read"): "all",
        ("organizations", "update"): "all",
        ("members", "list"): "all",
        ("members", "read"): "all",
        ("members", "update_status"): "all",
        ("members", "update_role"): "all",
        ("members", "import"): "all",
        ("members", "bulk"): "all",
        ("members", "revoke_key"): "all",
        ("usage", "read_org"): "all",
        ("usage", "read_self"): "self",
        ("works", "read_gallery"): "all",
        ("works", "read_own"): "self",
        ("works", "review"): "all",
        ("works", "feature"): "all",
        ("works", "unpublish"): "all",
        ("audit", "read"): "all",
        ("profile", "read_self"): "self",
        ("onboarding", "read"): "self",
        ("keys", "rotate_self"): "self",
        ("usage", "consume_self"): "self",
        ("sessions", "revoke_self"): "self",
        ("accounts", "list"): "all",
        ("accounts", "revoke_key"): "all",
        ("works", "read_any"): "all",
    },
    ORG_ADMIN: {
        ("organizations", "read"): "org",
        ("members", "list"): "org",
        ("members", "read"): "org",
        ("members", "update_status"): "org",
        ("members", "import"): "org",
        ("members", "revoke_key"): "org",
        ("usage", "read_org"): "org",
        ("usage", "read_self"): "self",
        ("works", "read_gallery"): "org",
        ("works", "read_own"): "self",
        ("works", "review"): "org",
        ("works", "feature"): "org",
        ("works", "unpublish"): "org",
        ("audit", "read"): "org",
        ("profile", "read_self"): "self",
        ("onboarding", "read"): "self",
        ("keys", "rotate_self"): "self",
        ("usage", "consume_self"): "self",
        ("sessions", "revoke_self"): "self",
        ("accounts", "list"): "org",
        ("accounts", "revoke_key"): "org",
    },
    ORG_MEMBER: {
        ("organizations", "read"): "org",
        ("usage", "read_self"): "self",
        ("works", "read_gallery"): "org",
        ("works", "read_own"): "self",
        ("works", "submit"): "self",
        ("works", "withdraw"): "self",
        ("onboarding", "read"): "self",
        ("onboarding", "write"): "self",
        ("keys", "read_self"): "self",
        ("keys", "rotate_self"): "self",
        ("profile", "read_self"): "self",
        ("usage", "consume_self"): "self",
        ("sessions", "revoke_self"): "self",
    },
}


def scope_for(role, resource, action):
    return _RULES.get(role, {}).get((resource, action))


def admission_blocked(status):
    return status in BLOCKED_ADMISSION


def principal_role(principal):
    return (principal or {}).get("role")


def allowed(principal, resource, action, org_id=None, owner_user_id=None):
    """判断是否授权。org_id 是目标资源所属组织，owner_user_id 是资源所有者。"""
    if not principal:
        return False
    role = principal_role(principal)
    scope = scope_for(role, resource, action)
    if scope is None:
        return False
    if scope == "all":
        return True
    if scope == "org":
        return bool(org_id) and principal.get("org_id") == org_id
    if scope == "self":
        return bool(owner_user_id) and principal.get("user_id") == owner_user_id
    return False


def require(principal, resource, action, org_id=None, owner_user_id=None):
    if not principal:
        raise AuthzError(401, "请先登录")
    if not allowed(principal, resource, action, org_id=org_id, owner_user_id=owner_user_id):
        # 跨组织与不存在的资源使用同一个状态和文案，避免泄露资源是否存在。
        raise AuthzError(403, "无权访问该资源")
    return True


def work_readable(principal, org_id, owner_user_id, published):
    """作品详情读取口径：本人任意状态、同组织已发布作品、平台管理员全部。"""
    if allowed(principal, "works", "read_own", owner_user_id=owner_user_id):
        return True
    if published and allowed(principal, "works", "read_gallery", org_id=org_id):
        return True
    return allowed(principal, "works", "read_any")


def matrix_rows():
    rows = []
    for role in ROLES:
        for (resource, action), scope in sorted(_RULES[role].items()):
            rows.append({"role": role, "resource": resource, "action": action, "scope": scope})
    return rows
