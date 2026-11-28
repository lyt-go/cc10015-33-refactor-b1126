"""成果汇编领域服务：汇总航次全部成果，接受数据传输完成后的数据完整度更新联动。

到件文本的登记口径（追加格式、状态范围、同名去重、实际受影响数量）
集中在本服务一处，数据传输模块确认完成后通过 apply_incoming_material 复用。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class ResultsCompileService(BaseModuleService):
    MODULE = "results_compile"
    ENTITY = "汇编报告"
    OPERATION_TERM = "提交汇编"
    LIST_FIELDS = ["航次编号", "成果类别", "主要发现", "参与人员", "数据完整度", "汇编人", "完成日期", "备注说明"]
    REQUIRED_FIELDS = ["航次编号", "成果类别", "主要发现"]
    STATUS_ORDER = ["汇编中", "已提交", "已审核", "需补充"]
    ACTION_RULES = {"提交汇编": "已提交", "审核通过": "已审核", "退回补充": "需补充"}
    NEGATIVE_ACTIONS = ["退回补充"]

    # 到件联动可更新的汇编状态范围（已审核不在其内，不可由到件动作修改）
    INCOMING_SYNC_STATUSES = ("汇编中", "已提交", "需补充")

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        target = self.ACTION_RULES.get(action)
        if target is None:
            return None, f"动作「{action}」不属于{self.MODULE}可执行范围"

        # 幂等检查
        idempotent_key = f"{self.MODULE}:{entry_id}:{action}"
        if store.is_idempotent(idempotent_key):
            return None, f"动作「{action}」已执行，请勿重复操作"

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("汇编中", "需补充")
        entry["abnormal"] = action in self.NEGATIVE_ACTIONS

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    def _append_completeness(self, entry: dict[str, Any], segment: str) -> None:
        """把一段到件文本按原格式追加到数据完整度，并标记待补全。"""
        current = entry.get("数据完整度", "")
        entry["数据完整度"] = f"{current} | {segment}"
        entry["pending"] = True

    def apply_incoming_material(self, data_content: str, transfer_time: str = "") -> int:
        """登记一份传输到件：对状态范围内的汇编报告追加「已接收」文本。

        同名材料按原口径去重（完整度文本中已出现则不再登记）；逐行原子写入，
        并发到件互不覆盖；返回本次实际受影响的报告数量。
        """
        updated_count = 0
        for row in list(store.rows(self.MODULE)):
            if row.get("status") not in self.INCOMING_SYNC_STATUSES:
                continue

            def apply(target: dict[str, Any]) -> bool:
                if data_content in str(target.get("数据完整度", "")):
                    return False
                self._append_completeness(target, f"已接收：{data_content}")
                target["transfer_complete"] = True
                target["last_transfer_time"] = transfer_time
                return True

            if store.atomic_mutate(self.MODULE, int(row.get("id", 0)), apply):
                updated_count += 1
        return updated_count

    def update_completeness(self, entry_id: int, data_source: str, data_desc: str) -> tuple[dict[str, Any] | None, str]:
        """由数据传输模块联动更新数据完整度（带来源的历史格式）。"""
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在"
        self._append_completeness(entry, f"[{data_source}] {data_desc}")
        return entry, "数据完整度已更新"

    def get_completeness_summary(self) -> dict[str, Any]:
        """获取整体数据完整度摘要。"""
        rows = store.rows(self.MODULE)
        total = len(rows)
        complete = sum(1 for r in rows if r.get("transfer_complete"))
        return {
            "total_reports": total,
            "complete_transfers": complete,
            "completeness_pct": round(complete / total * 100, 1) if total > 0 else 0,
        }
