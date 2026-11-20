"""演示名册：固定的学号到姓名映射，只做展示。

名册外的学号照常注册，姓名留空（不做准入）。学号从 2024001001 起连号，
用固定随机种子生成，保证每次启动一致。生产版把这里换成学校真实名册，
对外的 student_id_of 和 name_for 两个函数签名保持不变即可。
"""
import random

ROSTER_START = 2024001001
ROSTER_SIZE = 200
ROSTER_SEED = 20261119

_SURNAMES = "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林郑谢罗唐韩冯"
_GIVEN = "伟芳娜敏静强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英文辉华力"


def _build():
    rng = random.Random(ROSTER_SEED)
    roster = {}
    for i in range(ROSTER_SIZE):
        student_id = str(ROSTER_START + i)
        surname = rng.choice(_SURNAMES)
        given = rng.choice(_GIVEN)
        if rng.random() < 0.5:
            given += rng.choice(_GIVEN)
        roster[student_id] = surname + given
    return roster


ROSTER = _build()


def student_id_of(mode, identity):
    """从登录身份解析学号：学号模式就是本身，邮箱模式取 @ 前缀。解析不出返回 None。"""
    if not identity:
        return None
    if mode == "student":
        return identity
    if mode == "email" and "@" in identity:
        return identity.split("@", 1)[0]
    return None


def name_for(mode, identity):
    """按名册返回姓名，名册里没有就返回 None。"""
    student_id = student_id_of(mode, identity)
    if student_id is None:
        return None
    return ROSTER.get(student_id)


def is_enrolled(mode, identity):
    """登录身份是否在名册内。原型用这份名册模拟学校已认证的学生信息。"""
    student_id = student_id_of(mode, identity)
    return student_id is not None and student_id in ROSTER
