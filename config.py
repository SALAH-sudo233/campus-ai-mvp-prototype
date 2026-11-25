"""产品级配置。产品名称、试点组织和引导步骤只在这里定义。"""

PRODUCT_NAME = "校园 AI 创作平台"

PILOT_TOTAL_QUOTA = 20_000_000
DEFAULT_MEMBER_QUOTA = 100_000

PILOT_ORG = {
    "id": "org_hust_software",
    "slug": "hust-software",
    "name": "华中科技大学软件学院",
    "admin_label": "教师",
    "member_label": "学生",
}

ONBOARDING_FLOW_ID = "student-start"
ONBOARDING_FLOW_VERSION = 1
ONBOARDING_MAX_DRAFT = 16 * 1024

ONBOARDING_STEPS = [
    {
        "step_id": "team-basics",
        "title": "认识团队",
        "estimated_minutes": 2,
        "skippable": True,
        "blocks": [{"kind": "role-map"}, {"kind": "task-template"}],
        "primary_action": "confirm-team-template",
        "completion_rule": "team-template-confirmed",
    },
    {
        "step_id": "reuse-teams",
        "title": "选择工具",
        "estimated_minutes": 2,
        "skippable": True,
        "blocks": [{"kind": "product-choice"}],
        "primary_action": "confirm-product-choice",
        "completion_rule": "product-or-demo-selected",
    },
    {
        "step_id": "first-work",
        "title": "做出作品",
        "estimated_minutes": 7,
        "skippable": True,
        "blocks": [{"kind": "practice"}, {"kind": "video"}],
        "primary_action": "accept-practice",
        "completion_rule": "demo-checks-and-edit-confirmed",
    },
    {
        "step_id": "explore",
        "title": "探索平台",
        "estimated_minutes": 2,
        "skippable": True,
        "blocks": [{"kind": "destination-choice"}],
        "primary_action": "finish-and-open",
        "completion_rule": "destination-confirmed",
    },
]

ONBOARDING_VIDEO_URL = ""

SEED_WORKS = [
    {
        "work_id": "demo-campus-events",
        "title": "校园活动清单",
        "summary": "用虚构活动练习筛选和收藏。",
        "category": "活动",
        "preview_id": "campus-events-v1",
    },
    {
        "work_id": "demo-homework-list",
        "title": "小组作业清单",
        "summary": "添加事项、标记完成并查看剩余任务。",
        "category": "学习",
        "preview_id": "homework-list-v1",
    },
    {
        "work_id": "demo-focus-timer",
        "title": "自习番茄钟",
        "summary": "开始、暂停和重置一个专注计时器。",
        "category": "效率",
        "preview_id": "focus-timer-v1",
    },
    {
        "work_id": "demo-club-poster",
        "title": "社团活动海报页",
        "summary": "编辑标题、时间和活动介绍，预览窄屏布局。",
        "category": "活动",
        "preview_id": "club-poster-v1",
    },
]
