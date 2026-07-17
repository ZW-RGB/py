# -*- coding: utf-8 -*-
"""
智能模式 v5 精准度测试脚本

测试维度：
  1. 多维加权评分：精确/别名/部分/模糊匹配分层
  2. 同义词扩展："老人家"→"老人"，"看护"→"护理"
  3. 缩写展开："体检"→"能力评估"
  4. 字段消歧："老人的心率" → 心率监测（非老人护理）
  5. 否定排除："不要老人数据" → 排除老人相关端点
  6. 反馈学习：修正后相似查询自动 boost
  7. 回归测试：v3 已通过的用例仍然通过
"""
import sys
import os

# 设置路径
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from kangyang.intent_parser import (
    parse_intent, _rule_based_match_endpoint, _rule_based_extract_fields,
    _expand_query_with_synonyms, _extract_negated_terms, _check_field_endpoint_hint,
    ENDPOINT_ALIASES, FIELD_SYNONYMS, SYNONYM_GROUPS, ABBREVIATION_MAP,
)
from kangyang.query_feedback import record_query, get_feedback_boost, clear_feedback, get_endpoint_stats

# 确保测试前清空反馈历史
clear_feedback()

passed = 0
failed = 0
errors = []


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        errors.append(f"{name}: {detail}")
        print(f"  [FAIL] {name} -- {detail}")


def test_synonym_expansion():
    """测试同义词扩展"""
    print("\n═══════ 测试1: 同义词扩展 ═══════")

    # "老人家" 应扩展为包含 "老人"
    expanded = _expand_query_with_synonyms("老人家的心率")
    check("老人家→老人扩展", "老人" in expanded, f"expanded={expanded}")

    # "看护" 应扩展为包含 "护理"
    expanded = _expand_query_with_synonyms("看护记录")
    check("看护→护理扩展", "护理" in expanded, f"expanded={expanded}")

    # "住户" 应扩展为包含 "老人"
    expanded = _expand_query_with_synonyms("住户信息")
    check("住户→老人扩展", "老人" in expanded, f"expanded={expanded}")

    # "脉搏" 应扩展为包含 "心率"
    expanded = _expand_query_with_synonyms("脉搏数据")
    check("脉搏→心率扩展", "心率" in expanded, f"expanded={expanded}")


def test_abbreviation():
    """测试缩写展开"""
    print("\n═══════ 测试2: 缩写展开 ═══════")

    # "体检" 应映射到 "能力评估"
    check("体检∈缩写表", "体检" in ABBREVIATION_MAP)
    check("体检→能力评估", ABBREVIATION_MAP.get("体检") == "能力评估")

    # "护工" 应映射到 "护士信息"
    check("护工→护士信息", ABBREVIATION_MAP.get("护工") == "护士信息")


def test_negation():
    """测试否定排除"""
    print("\n═══════ 测试3: 否定排除 ═══════")

    # "不要老人数据" 应排除老人相关端点
    negated = _extract_negated_terms("不要老人数据")
    check("否定词提取-老人", "老人" in negated, f"negated={negated}")

    # "除了合同以外的信息" 应排除合同
    negated = _extract_negated_terms("除了合同以外的信息")
    check("否定词提取-合同", "合同" in negated, f"negated={negated}")

    # 正常查询不应有否定词
    negated = _extract_negated_terms("获取老人的心率数据")
    check("正常查询无否定", len(negated) == 0, f"negated={negated}")

    # 否定查询的端点匹配应排除对应端点
    candidates = _rule_based_match_endpoint("获取除了合同以外的所有数据")
    if candidates:
        for c in candidates:
            check(
                f"否定排除-合同端点被排除({c['endpoint']['name']})",
                "合同" not in c["endpoint"]["name"],
                f"name={c['endpoint']['name']}"
            )
            break
    else:
        check("否定排除-有候选", False, "no candidates")


def test_field_disambiguation():
    """测试字段消歧"""
    print("\n═══════ 测试4: 字段消歧 ═══════")

    # "老人的心率" 应优先匹配心率监测（而非老人护理/入住概览）
    candidates = _rule_based_match_endpoint("获取老人的心率数据")
    if candidates:
        best = candidates[0]
        check(
            "字段消歧-心率优先",
            "心率" in best["endpoint"]["name"] or "watchHeartrate" in best["endpoint"]["path"],
            f"best={best['endpoint']['name']}, score={best['score']}"
        )

    # "老人的血氧" 应优先匹配血氧监测
    candidates = _rule_based_match_endpoint("获取老人的血氧数据")
    if candidates:
        best = candidates[0]
        check(
            "字段消歧-血氧优先",
            "血氧" in best["endpoint"]["name"] or "watchOxygen" in best["endpoint"]["path"],
            f"best={best['endpoint']['name']}, score={best['score']}"
        )

    # "老人的身份证号" 应优先匹配入住概览
    candidates = _rule_based_match_endpoint("获取老人的身份证号")
    if candidates:
        best = candidates[0]
        check(
            "字段消歧-身份证→入住概览",
            "入住概览" in best["endpoint"]["name"] or "enrollmentView" in best["endpoint"]["path"],
            f"best={best['endpoint']['name']}, score={best['score']}"
        )

    # "合同的到期日期" 应优先匹配合同管理
    candidates = _rule_based_match_endpoint("获取合同的到期日期")
    if candidates:
        best = candidates[0]
        check(
            "字段消歧-合同到期→合同管理",
            "合同" in best["endpoint"]["name"] or "contract" in best["endpoint"]["path"],
            f"best={best['endpoint']['name']}, score={best['score']}"
        )


def test_multidimensional_scoring():
    """测试多维加权评分"""
    print("\n═══════ 测试5: 多维加权评分 ═══════")

    # 精确匹配端点名应得高分
    candidates = _rule_based_match_endpoint("入住概览")
    if candidates:
        check(
            "精确匹配-入住概览",
            candidates[0]["endpoint"]["name"] == "入住概览",
            f"got={candidates[0]['endpoint']['name']}"
        )
        check(
            "精确匹配高分>0.8",
            candidates[0]["score"] >= 0.8,
            f"score={candidates[0]['score']}"
        )

    # 语义匹配应优于部分匹配
    candidates = _rule_based_match_endpoint("氧气数据")
    if candidates:
        best = candidates[0]
        check(
            "语义匹配-氧气→血氧",
            "血氧" in best["endpoint"]["name"],
            f"got={best['endpoint']['name']}"
        )

    # 多关键词匹配应累加分数
    candidates = _rule_based_match_endpoint("老人档案的入住日期和联系电话")
    if candidates:
        check(
            "多关键词匹配-入住概览",
            "入住概览" in candidates[0]["endpoint"]["name"],
            f"got={candidates[0]['endpoint']['name']}"
        )


def test_feedback_learning():
    """测试反馈学习"""
    print("\n═══════ 测试6: 反馈学习 ═══════")

    # 清空历史
    clear_feedback()

    # 模拟一次错误匹配 + 用户修正
    fake_intent = {
        "target_api": "/dev-api/oldCare/oldCare/list",
        "api_name": "老人护理",
        "fields": [],
        "confidence": 0.5,
        "source_type": "api",
    }
    record_query(
        "获取老人的心跳数据", fake_intent,
        user_confirmed=False,
        corrected_endpoint="/dev-api/watchData/watchHeartrate/watchHeartrate/list",
        corrected_fields=["心率"],
    )

    # 再次查询相似内容，应该 boost 心率监测端点
    candidates = _rule_based_match_endpoint("获取老人的心跳数据")
    if candidates:
        # 找心率监测端点
        hr_cand = None
        for c in candidates:
            if "心率" in c["endpoint"]["name"] or "watchHeartrate" in c["endpoint"]["path"]:
                hr_cand = c
                break
        if hr_cand:
            check(
                "反馈boost-心率端点有加分",
                hr_cand.get("feedback_boost", 0) > 0,
                f"boost={hr_cand.get('feedback_boost', 0)}"
            )
            # 检查是否排在第一或接近第一
            check(
                "反馈boost-排名靠前",
                candidates.index(hr_cand) <= 1,
                f"rank={candidates.index(hr_cand)}"
            )
        else:
            check("反馈boost-心率端点存在", False, "心率端点未出现在候选中")

    # 检查统计
    stats = get_endpoint_stats()
    check("反馈统计-有记录", stats["total_queries"] > 0, f"total={stats['total_queries']}")

    # 清理
    clear_feedback()


def test_regression_v3():
    """v3 回归测试"""
    print("\n═══════ 测试7: v3 回归测试 ═══════")

    # 清空反馈历史，避免干扰回归测试
    clear_feedback()

    test_cases = [
        ("获取长者档案页面的所有长者姓名以及联系方式", "入住概览", ["联系方式"]),
        ("查看老人护理记录", "老人护理", []),
        ("获取合同管理列表的合同编号和签订日期", "合同管理", ["合同编号", "签订日期"]),
        ("查看血氧监测数据", "血氧监测", []),
        ("获取心率监测的心率值", "心率监测", ["心率"]),
        ("查看床位管理的房间号和床位号", "床位管理", ["房间号", "床位号"]),
        ("获取能力评估的评估结果", "能力评估", ["评估结果"]),
        ("查看活动管理的活动名称和活动日期", "活动管理", ["活动名称", "活动日期"]),
        ("获取护士信息的姓名", "护士信息", []),
        ("查看安全设备列表", "安全设备", []),
    ]

    for query, expected_ep_name, expected_fields in test_cases:
        result = parse_intent(query)
        if result and "error" not in result:
            check(
                f"回归-{query[:15]}...→{expected_ep_name}",
                result.get("api_name", "") == expected_ep_name,
                f"got={result.get('api_name', '?')}, expected={expected_ep_name}"
            )
            if expected_fields:
                actual_fields = result.get("fields", [])
                for ef in expected_fields:
                    check(
                        f"  字段-{ef}",
                        ef in actual_fields,
                        f"actual={actual_fields}"
                    )
        else:
            check(
                f"回归-{query[:15]}...",
                False,
                f"parse failed: {result.get('error', '?') if result else 'None'}"
            )


def test_edge_cases():
    """边界用例"""
    print("\n═══════ 测试8: 边界用例 ═══════")

    # 空查询
    result = parse_intent("")
    check("空查询处理", result is not None and "error" in result, f"result={result}")

    # 纯数字/无意义
    result = parse_intent("12345")
    check("无意义查询处理", result is None or result.get("confidence", 0) < 0.5, f"result={result}")

    # 超长查询
    long_query = "获取" + "老人" * 50 + "的姓名"
    result = parse_intent(long_query)
    check("超长查询不崩溃", result is not None, f"result={result}")

    # 同义词变体
    candidates = _rule_based_match_endpoint("老人家档案")
    if candidates:
        check(
            "同义词变体-老人家→入住概览",
            "入住概览" in candidates[0]["endpoint"]["name"],
            f"got={candidates[0]['endpoint']['name']}"
        )

    # "看护人员" 应匹配护士信息
    candidates = _rule_based_match_endpoint("看护人员名单")
    if candidates:
        check(
            "同义词-看护人员→护士信息",
            "护士" in candidates[0]["endpoint"]["name"] or "nurse" in candidates[0]["endpoint"]["path"].lower(),
            f"got={candidates[0]['endpoint']['name']}"
        )


def test_field_filtering():
    """测试字段过滤优化"""
    print("\n═══════ 测试9: 字段过滤优化 ═══════")

    # 否定字段应被排除
    fields = _rule_based_extract_fields("获取老人的姓名和身份证号，不要手机号")
    check("否定字段排除-手机号", "手机号" not in fields and "手机" not in fields, f"fields={fields}")
    check("保留字段-姓名", "姓名" in fields, f"fields={fields}")

    # 同义词标准化 —— "联系电话" 应标准化为 "联系方式"
    fields = _rule_based_extract_fields("获取联系电话")
    check("同义词标准化-联系电话→联系方式", "联系方式" in fields, f"fields={fields}")

    # 多字段提取
    fields = _rule_based_extract_fields("获取姓名、性别、年龄以及联系方式")
    check("多字段提取", len(fields) >= 3, f"fields={fields}")


if __name__ == "__main__":
    print("=" * 60)
    print("  Smart Mode v5 Accuracy Test")
    print("=" * 60)

    test_synonym_expansion()
    test_abbreviation()
    test_negation()
    test_field_disambiguation()
    test_multidimensional_scoring()
    test_feedback_learning()
    test_regression_v3()
    test_edge_cases()
    test_field_filtering()

    print("\n" + "=" * 60)
    print(f"  Result: {passed} passed, {failed} failed")
    if errors:
        print("\n  Failed details:")
        for e in errors:
            print(f"    [FAIL] {e}")
    print("=" * 60)

    sys.exit(0 if failed == 0 else 1)
