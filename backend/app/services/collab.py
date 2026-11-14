"""看板协作管理业务规则：版本化数据流、权限校验与发布结果保护都收在这里。

核心约定：
- 每次刷新频率（或成员范围）变更生成一个新的协作看板版本；
- 成员可见范围与看板发布结果挂在同一条版本记录上，读取时永远成对出现，不再分叉；
- 旧版本按当时口径保留（生效中 / 历史 / 已归档），供追溯与共享链接校验；
- 同一变更重复提交只推进一个版本（口径指纹幂等）；
- 并发修改先到先生效，后到请求读取已生效版本并收到冲突提示（先提交者优先）；
- 刷新失败只追加失败记录，不覆盖上一份完整发布结果。

版本保留策略：每个看板最多保留 5 个未归档版本（含生效中版本），
超出的最旧历史版本转为「已归档」只读留存，记录不删除，口径仍可追溯。
"""
from __future__ import annotations

import copy
import secrets
from typing import Any

from app.store import store
from app.versioning import format_members, now_str, parse_members, spec_fingerprint

MODULE = "collab"
VERSION_MODULE = "collab_versions"
REQUIRED_FIELDS = ["看板编号", "看板名称", "协作成员"]
STATUS_ORDER = ["草稿", "协作中", "已发布", "已归档"]
ACTION_RULES = {"发布看板": "草稿", "归档看板": "协作中", "删除看板": "已发布"}
NEGATIVE_ACTIONS = []

# 版本保留数量：每个看板最多保留 5 个未归档版本，超出的最旧历史版本归档留存。
VERSION_RETENTION = 5
# 平台管理员：与看板负责人同属「管理」角色，可调整配置与刷新发布结果。
ADMIN_OPERATORS = {"admin"}
# 成员范围收紧时共享链接的失效原因（受控失效，链接与原因都保留在版本记录里）。
LINK_INVALID_REASON = "成员范围收紧，旧共享链接已受控失效"


class CollabService:
    # ---------- 基础清单与登记 ----------

    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("看板编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["负责人"] = str(values.get("负责人") or "").strip()
        entry["图表集合"] = str(values.get("图表集合") or "").strip()
        entry["刷新频率"] = str(values.get("刷新频率") or "每日 08:00").strip()
        entry["评论开关"] = str(values.get("评论开关") or "开启").strip()
        entry["当前版本"] = 1
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        self._append_version(
            entry,
            frequency=entry["刷新频率"],
            members=parse_members(entry["协作成员"]),
            creator=entry["负责人"] or "系统",
            note="初始版本",
        )
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"协作看板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于看板协作管理可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"协作看板已{action}"

    # ---------- 版本化数据流 ----------

    def adjust_refresh_frequency(
        self,
        entry_id: int,
        *,
        operator: str,
        frequency: str,
        members: Any = None,
        base_version: int | None = None,
        note: str = "",
    ) -> tuple[dict[str, Any] | None, str, int]:
        """调整刷新频率：生成新版本，成员范围与发布结果随版本一起切换。

        返回 (结果, 说明, HTTP状态码)。幂等命中与并发冲突都不推进版本。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"协作看板 {entry_id} 不存在或已归档", 404
        role = self._role(entry, operator)
        if role != "管理":
            return None, self._deny_message(entry, operator, "调整刷新频率"), 403
        frequency = str(frequency or "").strip()
        if not frequency:
            return None, "刷新频率不能为空", 400

        current = self._effective_version(entry_id)
        if current is None:
            return None, f"协作看板 {entry_id} 缺少生效版本，数据异常", 500

        new_members = parse_members(members) if members is not None else list(current["成员范围"])
        if not new_members:
            return None, "协作成员不能为空，成员范围至少要保留一人", 400
        fingerprint = spec_fingerprint({
            "刷新频率": frequency,
            "成员范围": new_members,
            "图表集合": current["图表集合"],
            "评论开关": current["评论开关"],
        })

        # 幂等：口径与当前生效版本一致时不推进版本（重试、重复提交都安全）。
        if fingerprint == current.get("变更指纹"):
            return {
                "version": current,
                "created": False,
                "idempotent": True,
                "invalidated_links": 0,
            }, f"变更与当前生效版本 v{current['版本号']} 口径一致，未生成新版本", 200

        # 并发：先到先生效；后到请求基于过期版本号时，回读已生效版本并提示冲突。
        if base_version is not None and int(base_version) != int(current["版本号"]):
            return {
                "version": current,
                "created": False,
                "conflict": True,
            }, (
                f"看板已生效版本为 v{current['版本号']}，本次提交基于 v{base_version}，"
                "请读取已生效版本后重新提交"
            ), 409

        removed = [name for name in current["成员范围"] if name not in new_members]
        version = self._append_version(
            entry,
            frequency=frequency,
            members=new_members,
            creator=operator,
            note=note or f"刷新频率调整为「{frequency}」",
            fingerprint=fingerprint,
            previous=current,
        )
        current["版本状态"] = "历史"

        invalidated = 0
        if removed:
            invalidated = self._invalidate_share_links(entry_id, removed)

        archived = self._enforce_retention(entry_id)

        entry["刷新频率"] = frequency
        entry["协作成员"] = format_members(new_members)
        entry["当前版本"] = version["版本号"]

        message = (
            f"协作看板 {entry['看板编号']} 已生成版本 v{version['版本号']}，"
            f"成员可见范围与发布结果已切换到同一版本"
        )
        if invalidated:
            message += f"；成员范围收紧，{invalidated} 条旧共享链接已受控失效"
        if archived:
            message += f"；最早 {archived} 个历史版本已归档留存"
        return {
            "version": version,
            "created": True,
            "idempotent": False,
            "invalidated_links": invalidated,
            "archived_versions": archived,
        }, message, 200

    def refresh_publish_result(
        self,
        entry_id: int,
        *,
        operator: str,
        succeed: bool,
        reason: str = "",
    ) -> tuple[dict[str, Any] | None, str, int]:
        """刷新当前生效版本的发布结果；失败只记录，不覆盖上一份完整发布结果。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"协作看板 {entry_id} 不存在或已归档", 404
        role = self._role(entry, operator)
        if role != "管理":
            return None, self._deny_message(entry, operator, "刷新发布结果"), 403
        current = self._effective_version(entry_id)
        if current is None:
            return None, f"协作看板 {entry_id} 缺少生效版本，数据异常", 500

        if succeed:
            batch = int((current.get("最近成功发布") or {}).get("批次", 0)) + 1
            finished = now_str()
            result = {
                "发布状态": "成功",
                "版本号": current["版本号"],
                "批次": batch,
                "数据快照": f"批次{batch} · {finished} · 全量 {1280 + batch * 17} 行",
                "完成时间": finished,
                "来源": "本次刷新",
            }
            current["发布结果"] = result
            current["最近成功发布"] = copy.deepcopy(result)
            entry["abnormal"] = False
            return {
                "version": current,
                "publish": result,
            }, f"看板 {entry['看板编号']} 版本 v{current['版本号']} 发布结果已刷新（批次{batch}）", 200

        failure = {
            "时间": now_str(),
            "原因": str(reason or "未说明原因"),
            "操作人": operator,
        }
        current.setdefault("失败记录", []).append(failure)
        entry["abnormal"] = True
        kept = current.get("最近成功发布") or {}
        return {
            "version": current,
            "publish": current.get("发布结果"),
            "failure": failure,
        }, (
            f"刷新失败已记录（{failure['原因']}），版本 v{current['版本号']} 仍保留"
            f"上一份完整发布结果（批次{kept.get('批次', '—')}），未被覆盖"
        ), 200

    # ---------- 版本追溯 ----------

    def list_versions(
        self,
        entry_id: int,
        *,
        operator: str,
    ) -> tuple[list[dict[str, Any]] | None, str, int]:
        """版本清单：负责人/管理员/协作成员可追溯全部版本，访客无权查看。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"协作看板 {entry_id} 不存在或已归档", 404
        role = self._role(entry, operator)
        if role not in ("管理", "成员"):
            return None, self._deny_message(entry, operator, "查看版本记录"), 403
        versions = sorted(self._versions_of(entry_id), key=lambda v: int(v["版本号"]), reverse=True)
        return versions, f"共 {len(versions)} 个版本（含已归档），均按当时口径保留", 200

    # ---------- 共享链接 ----------

    def create_share_link(
        self,
        entry_id: int,
        *,
        operator: str,
    ) -> tuple[dict[str, Any] | None, str, int]:
        """为当前生效版本签发共享链接；链接跟随版本，成员收紧时受控失效。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"协作看板 {entry_id} 不存在或已归档", 404
        role = self._role(entry, operator)
        if role != "管理":
            return None, self._deny_message(entry, operator, "签发共享链接"), 403
        current = self._effective_version(entry_id)
        if current is None:
            return None, f"协作看板 {entry_id} 缺少生效版本，数据异常", 500
        link = {
            "令牌": f"{entry['看板编号']}-v{current['版本号']}-{secrets.token_hex(3)}",
            "状态": "有效",
            "失效原因": None,
            "创建时间": now_str(),
            "创建人": operator,
        }
        current.setdefault("共享链接", []).append(link)
        return {"link": link, "version": current["版本号"]}, (
            f"已为看板 {entry['看板编号']} 版本 v{current['版本号']} 签发共享链接"
        ), 200

    def get_shared(self, token: str) -> tuple[dict[str, Any] | None, str, int]:
        """按令牌读取共享内容：只读视图；链接失效时给出受控失效说明（410）。"""
        token = str(token or "").strip()
        if not token:
            return None, "共享链接令牌不能为空", 400
        for version in store.rows(VERSION_MODULE):
            for link in version.get("共享链接", []):
                if link.get("令牌") != token:
                    continue
                if link.get("状态") != "有效":
                    reason = link.get("失效原因") or LINK_INVALID_REASON
                    return None, f"共享链接已失效：{reason}", 410
                entry = store.find(MODULE, int(version["看板id"]))
                return {
                    "看板编号": version["看板编号"],
                    "看板名称": (entry or {}).get("看板名称", ""),
                    "版本号": version["版本号"],
                    "刷新频率": version["刷新频率"],
                    "图表集合": version["图表集合"],
                    "发布结果": version.get("发布结果"),
                    "只读": True,
                    "链接创建时间": link.get("创建时间"),
                }, f"共享链接有效，正在查看版本 v{version['版本号']} 的只读内容", 200
        return None, "共享链接不存在或已被清理", 404

    # ---------- 导出（权限与归属校验） ----------

    def export_entry(
        self,
        entry_id: int,
        *,
        operator: str,
    ) -> tuple[dict[str, Any] | None, str, int]:
        """导出入口：按操作人角色返回口径一致的发布结果，导出本身不改动任何数据。

        - 负责人/管理员/协作成员：导出当前生效版本的完整口径与发布结果；
        - 访客：只能拿到只读版本（不含成员名单、失败记录与共享链接）；
        - 未表明身份：拒绝导出。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"协作看板 {entry_id} 不存在或已归档", 404
        if not operator:
            return None, "导出入口要求通过 operator 表明操作人身份", 403
        current = self._effective_version(entry_id)
        if current is None:
            return None, f"协作看板 {entry_id} 缺少生效版本，数据异常", 500
        role = self._role(entry, operator, current)

        if role == "访客":
            return {
                "module": "collab",
                "看板编号": entry["看板编号"],
                "看板名称": entry["看板名称"],
                "当前版本": current["版本号"],
                "刷新频率": current["刷新频率"],
                "发布结果": current.get("发布结果"),
                "只读": True,
                "权限": "访客",
                "导出人": operator,
                "导出时间": now_str(),
            }, "访客仅可查看只读版本，已隐藏成员范围与共享链接", 200

        return {
            "module": "collab",
            "看板编号": entry["看板编号"],
            "看板名称": entry["看板名称"],
            "负责人": entry.get("负责人"),
            "当前版本": current["版本号"],
            "刷新频率": current["刷新频率"],
            "协作成员": list(current["成员范围"]),
            "图表集合": current["图表集合"],
            "评论开关": current["评论开关"],
            "发布结果": current.get("发布结果"),
            "最近成功发布": current.get("最近成功发布"),
            "失败记录": list(current.get("失败记录", [])),
            "共享链接": [
                {"令牌": link["令牌"], "状态": link["状态"], "失效原因": link.get("失效原因")}
                for link in current.get("共享链接", [])
            ],
            "只读": False,
            "权限": role,
            "导出人": operator,
            "导出时间": now_str(),
        }, f"已按「{role}」权限导出版本 v{current['版本号']} 的发布结果", 200

    # ---------- 内部工具 ----------

    def _versions_of(self, entry_id: int) -> list[dict[str, Any]]:
        return [row for row in store.rows(VERSION_MODULE) if int(row.get("看板id", 0)) == entry_id]

    def _effective_version(self, entry_id: int) -> dict[str, Any] | None:
        for version in self._versions_of(entry_id):
            if version.get("版本状态") == "生效中":
                return version
        return None

    def _role(
        self,
        entry: dict[str, Any],
        operator: str,
        version: dict[str, Any] | None = None,
    ) -> str | None:
        """操作人角色：管理（负责人/平台管理员）、成员（当前版本可见范围内）、访客。"""
        operator = str(operator or "").strip()
        if not operator:
            return None
        if operator in ADMIN_OPERATORS or operator == str(entry.get("负责人") or "").strip():
            return "管理"
        version = version or self._effective_version(int(entry.get("id", 0)))
        if version and operator in version.get("成员范围", []):
            return "成员"
        return "访客"

    def _deny_message(self, entry: dict[str, Any], operator: str, action: str) -> str:
        operator = str(operator or "").strip()
        if not operator:
            return f"{action}需要先通过 operator 表明操作人身份"
        role = self._role(entry, operator)
        if role == "访客":
            return f"操作人「{operator}」不在看板 {entry['看板编号']} 的协作范围内，{action}已被拦截，已发布结果未被改动"
        return f"操作人「{operator}」是看板 {entry['看板编号']} 的协作成员，仅负责人或管理员可{action}，已发布结果未被改动"

    def _append_version(
        self,
        entry: dict[str, Any],
        *,
        frequency: str,
        members: list[str],
        creator: str,
        note: str,
        fingerprint: str | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """追加一条版本记录；发布结果从上一版本继承并改挂到新版本号，保证口径一致。"""
        rows = store.rows(VERSION_MODULE)
        next_no = max((int(v["版本号"]) for v in self._versions_of(int(entry["id"]))), default=0) + 1
        if previous and previous.get("最近成功发布"):
            inherited = copy.deepcopy(previous["最近成功发布"])
            inherited["版本号"] = next_no
            inherited["来源"] = f"继承自版本 v{previous['版本号']}"
            last_good = copy.deepcopy(previous["最近成功发布"])
        else:
            inherited = None
            last_good = None
        version = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "pending": False,
            "abnormal": False,
            "看板id": int(entry["id"]),
            "看板编号": entry["看板编号"],
            "版本号": next_no,
            "版本状态": "生效中",
            "刷新频率": frequency,
            "成员范围": list(members),
            "图表集合": entry.get("图表集合", ""),
            "评论开关": entry.get("评论开关", ""),
            "变更指纹": fingerprint or spec_fingerprint({
                "刷新频率": frequency,
                "成员范围": members,
                "图表集合": entry.get("图表集合", ""),
                "评论开关": entry.get("评论开关", ""),
            }),
            "发布结果": inherited,
            "最近成功发布": last_good,
            "失败记录": [],
            "共享链接": [],
            "创建人": creator,
            "创建时间": now_str(),
            "变更说明": note,
        }
        rows.append(version)
        return version

    def _invalidate_share_links(self, entry_id: int, removed: list[str]) -> int:
        """成员范围收紧后，该看板所有有效共享链接受控失效（保留记录供追溯）。"""
        invalidated = 0
        for version in self._versions_of(entry_id):
            for link in version.get("共享链接", []):
                if link.get("状态") == "有效":
                    link["状态"] = "已失效"
                    link["失效原因"] = LINK_INVALID_REASON
                    link["失效时间"] = now_str()
                    invalidated += 1
        return invalidated

    def _enforce_retention(self, entry_id: int) -> int:
        """版本保留策略：最多保留 VERSION_RETENTION 个未归档版本，最旧的历史版本归档。"""
        active = [v for v in self._versions_of(entry_id) if v.get("版本状态") != "已归档"]
        overflow = len(active) - VERSION_RETENTION
        if overflow <= 0:
            return 0
        archived = 0
        effective = self._effective_version(entry_id)
        candidates = sorted(
            (v for v in active if v is not effective),
            key=lambda v: int(v["版本号"]),
        )
        for version in candidates[:overflow]:
            version["版本状态"] = "已归档"
            archived += 1
        return archived
