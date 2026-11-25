"""表驱动的服务端权限矩阵。前端隐藏按钮不作为授权依据。"""

PLATFORM_ADMIN = "platform_admin"
ORG_ADMIN = "org_admin"
ORG_MEMBER = "org_member"
ROLES = (PLATFORM_ADMIN, ORG_ADMIN, ORG_MEMBER)

# 准入状态闸：disabled 视同未认证；pending_review 只保留登录、个人信息（含余额流水）、
# 引导和登出，其余动作一律 403。准入判断和角色判断在同一个矩阵模块内完成。
BLOCKED_ADMISSION = ("disabled",)
PENDING_REVIEW_ALLOWED = frozenset({
    ("profile", "read_self"),
    ("usage", "read_self"),
    ("onboarding", "read"),
    ("onboarding", "write"),
    ("sessions", "revoke_self"),
})
PENDING_REVIEW_MESSAGE = "账户待复核，复核通过后开放"


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
        ("members", "reissue_key"): "all",
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
        ("accounts", "reissue_key"): "all",
        ("works", "read_any"): "all",
        ("rewards", "read"): "all",
        ("rewards", "settle"): "all",
    },
    ORG_ADMIN: {
        ("organizations", "read"): "org",
        ("members", "list"): "org",
        ("members", "read"): "org",
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
        ("accounts", "reissue_key"): "org",
        ("members", "revoke_key"): "org",
        ("members", "reissue_key"): "org",
        ("rewards", "read"): "org",
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
        ("rewards", "read"): "org",
    },
}


def scope_for(role, resource, action):
    return _RULES.get(role, {}).get((resource, action))


def admission_blocked(status):
    return status in BLOCKED_ADMISSION


def admission_restricted(principal):
    """待复核只约束非平台主体的成员动作；平台管理员会话不挂在组织准入状态上。"""
    if not principal or principal.get("kind") == "platform":
        return False
    return principal.get("admission_status") == "pending_review"


def admission_allows(principal, resource, action):
    return not admission_restricted(principal) or (resource, action) in PENDING_REVIEW_ALLOWED


def principal_role(principal):
    return (principal or {}).get("role")


def _scope_allows(principal, scope, org_id, owner_user_id):
    if scope == "all":
        return True
    if scope == "org":
        return bool(org_id) and principal.get("org_id") == org_id
    if scope == "self":
        return bool(owner_user_id) and principal.get("user_id") == owner_user_id
    return False


def allowed(principal, resource, action, org_id=None, owner_user_id=None):
    """判断是否授权。org_id 是目标资源所属组织，owner_user_id 是资源所有者。"""
    if not principal:
        return False
    if not admission_allows(principal, resource, action):
        return False
    scope = scope_for(principal_role(principal), resource, action)
    return scope is not None and _scope_allows(principal, scope, org_id, owner_user_id)


def require(principal, resource, action, org_id=None, owner_user_id=None):
    if not principal:
        raise AuthzError(401, "请先登录")
    scope = scope_for(principal_role(principal), resource, action)
    if scope is None or not _scope_allows(principal, scope, org_id, owner_user_id):
        # 跨组织与不存在的资源使用同一个状态和文案，避免泄露资源是否存在。
        raise AuthzError(403, "无权访问该资源")
    if not admission_allows(principal, resource, action):
        raise AuthzError(403, PENDING_REVIEW_MESSAGE)
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
