"""协作看板版本口径工具：成员范围解析、口径指纹与时间戳。

看板配置、成员可见范围、发布结果挂在同一条版本记录上，指纹只由口径字段决定，
保证“同一变更重复提交只推进一个版本”的幂等判断与存储、种子数据使用同一套算法。
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime
from typing import Any

_MEMBER_SEPARATORS = re.compile(r"[、,，;；\s]+")


def parse_members(value: Any) -> list[str]:
    """把「陈晨、李蔚」这类成员写法规整成去重后的名单；列表输入同样归一化。"""
    if value is None:
        return []
    parts = value if isinstance(value, list) else _MEMBER_SEPARATORS.split(str(value))
    members: list[str] = []
    for part in parts:
        name = str(part).strip()
        if name and name not in members:
            members.append(name)
    return members


def format_members(members: list[str]) -> str:
    """成员名单的展示口径：顿号连接，与看板清单字段保持一致。"""
    return "、".join(members)


def spec_fingerprint(spec: dict[str, Any]) -> str:
    """口径指纹：只看刷新频率、成员范围、图表集合、评论开关，与版本号、时间无关。"""
    canonical = {
        "刷新频率": str(spec.get("刷新频率") or "").strip(),
        "成员范围": sorted(parse_members(spec.get("成员范围"))),
        "图表集合": str(spec.get("图表集合") or "").strip(),
        "评论开关": str(spec.get("评论开关") or "").strip(),
    }
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
