"""内存数据仓库：给每个业务模块准备一份可筛选、可流转的示例数据。

真实项目里这里会换成数据库访问层；当前实现只依赖标准库，保证克隆下来就能起。
增加幂等检查、并发锁、跨模块状态同步、重试恢复和回滚能力。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from app.seed import SEED_ROWS


class Store:
    def __init__(self) -> None:
        self._tables: dict[str, list[dict[str, Any]]] = {}
        self._idempotent_keys: set[str] = set()
        self._locks: dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        # 回滚日志：{transaction_id: {module, entry_id, before_snapshot}}
        self._rollback_log: dict[str, dict] = {}
        # 重试配置
        self._retry_max: int = 3
        self._retry_delay: float = 0.01
        self._seed()

    def _seed(self) -> None:
        self._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        self._idempotent_keys.clear()
        self._rollback_log.clear()

    def reset(self) -> None:
        """重置所有数据回到初始种子状态。"""
        self._seed()
        self._locks.clear()

    def _get_lock(self, key: str) -> threading.Lock:
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def module_names(self) -> list[str]:
        return sorted(self._tables)

    def rows(self, module: str) -> list[dict[str, Any]]:
        return self._tables.setdefault(module, [])

    def find(self, module: str, entry_id: int) -> dict[str, Any] | None:
        for row in self.rows(module):
            if int(row.get("id", 0)) == entry_id:
                return row
        return None

    def atomic_update(self, module: str, entry_id: int, updates: dict[str, Any]) -> dict[str, Any] | None:
        """原子更新一条记录，返回更新后的记录或None。"""
        lock = self._get_lock(f"{module}:{entry_id}")
        with lock:
            row = self.find(module, entry_id)
            if row is None:
                return None
            row.update(updates)
            return dict(row)

    def atomic_mutate(self, module: str, entry_id: int, mutate) -> bool:
        """在行锁内对记录执行条件修改；mutate 返回真值表示实际发生变更。"""
        lock = self._get_lock(f"{module}:{entry_id}")
        with lock:
            row = self.find(module, entry_id)
            if row is None:
                return False
            return bool(mutate(row))

    def atomic_append(self, module: str, entry: dict[str, Any]) -> dict[str, Any]:
        """原子追加一条记录。"""
        lock = self._get_lock(f"{module}:append")
        with lock:
            rows = self.rows(module)
            entry_copy = dict(entry)
            rows.append(entry_copy)
            return entry_copy

    def is_idempotent(self, key: str) -> bool:
        """检查幂等键是否已存在（不写入）。"""
        with self._global_lock:
            return key in self._idempotent_keys

    def mark_idempotent(self, key: str) -> None:
        """标记幂等键为已执行。"""
        with self._global_lock:
            self._idempotent_keys.add(key)

    def clear_idempotent_key(self, key: str) -> None:
        """清除幂等键（用于失败回滚）。"""
        with self._global_lock:
            self._idempotent_keys.discard(key)

    def find_by(self, module: str, field: str, value: Any) -> list[dict[str, Any]]:
        """按字段值查找记录。"""
        rows = self.rows(module)
        if value is None:
            return [row for row in rows if row.get(field) is None]
        return [row for row in rows if str(row.get(field, "")) == str(value)]

    def find_one_by(self, module: str, field: str, value: Any) -> dict[str, Any] | None:
        """按字段值查找第一条记录。"""
        results = self.find_by(module, field, value)
        return results[0] if results else None

    def snapshot(self, module: str) -> list[dict[str, Any]]:
        """返回模块的当前快照（浅拷贝）。"""
        return [dict(row) for row in self.rows(module)]

    def save_snapshot(self, transaction_id: str, module: str, entry_id: int) -> str:
        """保存操作前的快照，支持失败后回滚。"""
        row = self.find(module, entry_id)
        key = f"{module}:{entry_id}"
        if row is None:
            return transaction_id
        with self._global_lock:
            self._rollback_log[transaction_id] = {
                "module": module,
                "entry_id": entry_id,
                "before": dict(row),
            }
        return transaction_id

    def rollback(self, transaction_id: str) -> bool:
        """回滚到指定事务前的状态。"""
        with self._global_lock:
            log = self._rollback_log.pop(transaction_id, None)
            if log is None:
                return False
            row = self.find(log["module"], log["entry_id"])
            if row is not None:
                row.clear()
                row.update(log["before"])
        return True

    def retry(self, func, *args, max_retries: int | None = None, **kwargs) -> tuple[Any, bool]:
        """带重试机制执行操作。返回(结果, 是否成功)。"""
        retries = max_retries if max_retries is not None else self._retry_max
        last_error = None
        for attempt in range(retries):
            try:
                result = func(*args, **kwargs)
                return result, True
            except Exception as e:
                last_error = e
                if attempt < retries - 1:
                    time.sleep(self._retry_delay * (attempt + 1))
        return None, False

    def overview(self) -> dict[str, object]:
        modules: list[dict[str, object]] = []
        for name in self.module_names():
            rows = self.rows(name)
            modules.append({
                "name": name,
                "created": len(rows),
                "pending": sum(1 for row in rows if row.get("pending")),
                "abnormal": sum(1 for row in rows if row.get("abnormal")),
            })
        cards = [
            {"label": "业务模块", "value": len(modules)},
            {"label": "今日新增", "value": sum(int(item["created"]) for item in modules)},
            {"label": "待处理", "value": sum(int(item["pending"]) for item in modules)},
            {"label": "异常量", "value": sum(int(item["abnormal"]) for item in modules)},
        ]
        return {"cards": cards, "modules": modules}


store = Store()