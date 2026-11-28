"""数据传输领域服务：完成传输后自动联动成果汇编更新数据完整度。
支持事务快照回滚和重试机制；到件文本的登记口径归拢在成果汇编服务，
联动全部写完才标记幂等，中断时保留 confirm_syncing 供恢复后继续确认。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.results_compile import ResultsCompileService
from app.store import store


class DataTransferService(BaseModuleService):
    MODULE = "data_transfer"
    ENTITY = "传输记录"
    OPERATION_TERM = "发起传输"
    LIST_FIELDS = ["数据内容", "数据量", "传输方式", "目标系统", "传输状态", "操作人", "传输时间", "备注说明"]
    REQUIRED_FIELDS = ["数据内容", "数据量", "传输方式"]
    STATUS_ORDER = ["待传输", "传输中", "已完成", "已失败"]
    ACTION_RULES = {"发起传输": "传输中", "确认完成": "已完成", "重试传输": "待传输"}
    NEGATIVE_ACTIONS = []

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

        # 重试传输：只能从失败状态重试
        if action == "重试传输" and entry.get("status") != "已失败":
            return None, "只能重试已失败的传输"

        # 保存快照
        tx_id = f"{self.MODULE}:{entry_id}:{action}:tx"
        store.save_snapshot(tx_id, self.MODULE, entry_id)

        old_status = entry.get("status")
        entry["status"] = target
        entry["pending"] = target in ("待传输", "传输中")
        entry["abnormal"] = False

        if action == "重试传输":
            entry["retry_count"] = entry.get("retry_count", 0) + 1
            entry["last_retry_time"] = str(Any)

        # 传输完成后联动成果汇编登记到件；联动全部写完才标记幂等，
        # 中断时 confirm_syncing 保持为真，恢复后可用原传输继续确认。
        if action == "确认完成":
            entry["confirm_syncing"] = True
            updated_count = ResultsCompileService().apply_incoming_material(
                entry.get("数据内容", ""), entry.get("传输时间", ""))
            entry["confirm_syncing"] = False
            # 回执只反映本次实际采用的到件所影响的报告数量
            entry["compile_updates"] = updated_count

        store.mark_idempotent(idempotent_key)

        return entry, f"{self.ENTITY}已{action}"

    def pending_confirmations(self) -> list[dict[str, Any]]:
        """值班员恢复查询：到件联动尚未写完、需要继续确认的传输记录。"""
        return [row for row in store.rows(self.MODULE) if row.get("confirm_syncing")]
