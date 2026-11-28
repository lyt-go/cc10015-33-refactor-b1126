# -*- coding: utf-8 -*-
"""传输到件与成果汇编完整度联动测试。

归拢后的登记口径须保持原行为：
- 确认完成向状态范围内的汇编报告追加「已接收：数据内容」，回执给出本次实际受影响报告数；
- 汇编服务自带来源的历史格式「[来源] 描述」保留，两种条目可按原格式查询；
- 已审核报告不可由到件动作修改；
并覆盖：两份材料并发确认、写甲后中断再用原传输恢复、同名材料去重。
"""
from __future__ import annotations

import concurrent.futures

import pytest

from app.store import store
from app.services.data_transfer import DataTransferService
from app.services.results_compile import ResultsCompileService


@pytest.fixture(autouse=True)
def reset_store():
    """每个测试前重置store到初始种子状态。"""
    store.reset()


def _transfer(idx: int, **kw) -> dict:
    row = store.rows("data_transfer")[idx]
    row.update(kw)
    return row


def _compile_rows() -> list:
    return store.rows("results_compile")


# ============================================================
# 原口径保持：回执数量、正文格式、状态范围
# ============================================================
def test_confirm_completion_keeps_original_receipt_and_formats():
    transfer_svc = DataTransferService()
    _transfer(0, status="传输中", 数据内容="CTD剖面数据V3", 传输时间="2026-11-25")

    entry, msg = transfer_svc.run_action(1, "确认完成")
    assert entry is not None, f"确认失败: {msg}"
    assert entry["status"] == "已完成"
    # 回执给出本次实际受影响的报告数量：汇编中、已提交两份在范围内，已审核不在
    assert entry["compile_updates"] == 2
    assert entry["confirm_syncing"] is False

    rows = _compile_rows()
    jia, yi, reviewed = rows[0], rows[1], rows[2]
    for row in (jia, yi):
        assert row["数据完整度"].endswith(" | 已接收：CTD剖面数据V3")
        assert row["transfer_complete"] is True
        assert row["last_transfer_time"] == "2026-11-25"
        assert row["pending"] is True
    # 已审核报告不可由到件动作修改
    assert reviewed["数据完整度"] == "成果汇编样例3"
    assert not reviewed.get("transfer_complete")

    # 汇编服务带来源的历史条目格式保留，两种条目可按原格式共存查询
    compile_svc = ResultsCompileService()
    updated, msg = compile_svc.update_completeness(1, "数据质控", "异常值已修正")
    assert updated is not None, msg
    assert " | [数据质控] 异常值已修正" in jia["数据完整度"]
    assert "已接收：CTD剖面数据V3" in jia["数据完整度"]

    summary = compile_svc.get_completeness_summary()
    assert summary["total_reports"] == 3
    assert summary["complete_transfers"] == 2


def test_incoming_scope_covers_needs_supplement_but_not_reviewed():
    """状态范围保持原口径：汇编中/已提交/需补充更新，已审核不更新。"""
    rows = _compile_rows()
    rows.append({
        "id": 4, "航次编号": "RESU-0004", "成果类别": "成果汇编样例4",
        "主要发现": "成果汇编样例4", "参与人员": "成果汇编样例4",
        "数据完整度": "成果汇编样例4", "汇编人": "成果汇编样例4",
        "完成日期": "2026-09-04", "备注说明": "成果汇编样例4",
        "status": "需补充", "pending": True, "abnormal": False,
    })
    transfer_svc = DataTransferService()
    _transfer(0, status="传输中", 数据内容="走航气象观测集", 传输时间="2026-11-26")

    entry, msg = transfer_svc.run_action(1, "确认完成")
    assert entry is not None, f"确认失败: {msg}"
    assert entry["compile_updates"] == 3

    rows = _compile_rows()
    assert "已接收：走航气象观测集" in rows[3]["数据完整度"]
    assert rows[2]["数据完整度"] == "成果汇编样例3"  # 已审核不受影响
    assert not rows[2].get("transfer_complete")


# ============================================================
# 同一轮两份材料并发确认，写入甲乙两个汇编报告
# ============================================================
def test_concurrent_confirm_two_materials_both_applied():
    transfer_svc = DataTransferService()
    _transfer(0, status="传输中", 数据内容="航迹资料包", 传输时间="2026-11-25")
    _transfer(1, status="传输中", 数据内容="气象资料包", 传输时间="2026-11-25")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(transfer_svc.run_action, 1, "确认完成"),
            executor.submit(transfer_svc.run_action, 2, "确认完成"),
        ]
        results = [f.result() for f in futures]

    for entry, msg in results:
        assert entry is not None, f"并发确认失败: {msg}"
        assert entry["compile_updates"] == 2

    rows = _compile_rows()
    for row in rows[:2]:  # 甲、乙
        for material in ("航迹资料包", "气象资料包"):
            assert row["数据完整度"].count(f"已接收：{material}") == 1, (
                f"{material} 应恰好登记一次，实际: {row['数据完整度']}")
        assert row["transfer_complete"] is True
    # 已审核报告未被并发到件触碰
    assert rows[2]["数据完整度"] == "成果汇编样例3"


# ============================================================
# 写甲之后模拟断开，值班员恢复查询并用原传输继续确认
# ============================================================
def test_interrupt_after_first_report_then_resume(monkeypatch):
    transfer_svc = DataTransferService()
    compile_svc = ResultsCompileService()
    _transfer(0, status="传输中", 数据内容="航迹资料包", 传输时间="2026-11-25")
    _transfer(1, status="传输中", 数据内容="气象资料包", 传输时间="2026-11-25")

    # 模拟链路断开：第二次行写入（即写乙）时抛错，甲已写完
    original_mutate = store.atomic_mutate
    calls = {"n": 0}

    def flaky_mutate(module, entry_id, mutate):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟链路断开")
        return original_mutate(module, entry_id, mutate)

    monkeypatch.setattr(store, "atomic_mutate", flaky_mutate)

    # 确认航迹：写甲之后断开
    with pytest.raises(RuntimeError, match="模拟链路断开"):
        transfer_svc.run_action(1, "确认完成")

    rows = _compile_rows()
    jia, yi, reviewed = rows[0], rows[1], rows[2]
    assert "已接收：航迹资料包" in jia["数据完整度"]      # 材料已到甲
    assert "航迹资料包" not in yi["数据完整度"]           # 还没到乙

    # 值班员恢复时能查到哪份传输需要继续确认
    pending = transfer_svc.pending_confirmations()
    assert [row["id"] for row in pending] == [1]

    # 审核不会把尚未写完的乙当作资料完整：完整度摘要只认实际办结的甲
    summary = compile_svc.get_completeness_summary()
    assert summary["complete_transfers"] == 1
    assert not yi.get("transfer_complete")

    # 同一轮第二份材料（气象）确认不受影响，甲乙均实际登记
    entry, msg = transfer_svc.run_action(2, "确认完成")
    assert entry is not None, f"气象确认失败: {msg}"
    assert entry["compile_updates"] == 2

    # 用原传输重试继续确认：只补乙，甲按同名口径不重复登记
    entry, msg = transfer_svc.run_action(1, "确认完成")
    assert entry is not None, f"恢复确认失败: {msg}"
    assert entry["status"] == "已完成"
    assert entry["compile_updates"] == 1, "本次实际只补写乙一份"
    assert entry["confirm_syncing"] is False
    assert transfer_svc.pending_confirmations() == []

    # 查看甲、乙和已审核报告的完整度
    assert jia["数据完整度"].count("已接收：航迹资料包") == 1
    assert jia["数据完整度"].count("已接收：气象资料包") == 1
    assert yi["数据完整度"].count("已接收：航迹资料包") == 1
    assert yi["数据完整度"].count("已接收：气象资料包") == 1
    assert reviewed["数据完整度"] == "成果汇编样例3"
    assert not reviewed.get("transfer_complete")

    # 已办结的确认动作不可重复执行
    again, msg = transfer_svc.run_action(1, "确认完成")
    assert again is None
    assert "重复" in msg

    # 重复提交同名材料：按原去重口径只记一次，回执可核对本次实际更新数为0
    dup, missing = transfer_svc.create_entry(
        {"数据内容": "航迹资料包", "数据量": "1GB", "传输方式": "卫星"})
    assert not missing
    entry, msg = transfer_svc.run_action(dup["id"], "确认完成")
    assert entry is not None, f"同名材料确认失败: {msg}"
    assert entry["compile_updates"] == 0, "同名材料均已登记过，本次无实际采用"
    assert jia["数据完整度"].count("已接收：航迹资料包") == 1
    assert yi["数据完整度"].count("已接收：航迹资料包") == 1
