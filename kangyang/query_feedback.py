# -*- coding: utf-8 -*-
"""
查询反馈学习引擎 v1

功能：
  1. 记录每次查询的解析结果（query → endpoint + fields + confidence）
  2. 记录用户修正行为（用户确认/修正了哪个端点）
  3. 基于历史数据动态调整匹配权重
  4. 当相似查询再次出现时，自动 boost 历史确认的端点

存储: JSON 文件，位于 output/feedback/feedback_history.json
"""
import json
import os
import re
import logging
from datetime import datetime
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)

# ── 反馈存储路径 ──
_FEEDBACK_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "output", "feedback"
)
_FEEDBACK_FILE = os.path.join(_FEEDBACK_DIR, "feedback_history.json")

# ── 内存缓存 ──
_feedback_cache = None
_last_load_time = 0
_CACHE_TTL = 5  # 5 秒缓存


def _ensure_dir():
    os.makedirs(_FEEDBACK_DIR, exist_ok=True)


def _load_feedback():
    """加载反馈历史（带缓存）"""
    global _feedback_cache, _last_load_time
    now = datetime.now().timestamp()
    if _feedback_cache is not None and (now - _last_load_time) < _CACHE_TTL:
        return _feedback_cache

    if not os.path.exists(_FEEDBACK_FILE):
        _feedback_cache = {"records": [], "stats": {}}
        return _feedback_cache

    try:
        with open(_FEEDBACK_FILE, "r", encoding="utf-8") as f:
            _feedback_cache = json.load(f)
            if "records" not in _feedback_cache:
                _feedback_cache["records"] = []
            if "stats" not in _feedback_cache:
                _feedback_cache["stats"] = {}
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"加载反馈历史失败: {e}")
        _feedback_cache = {"records": [], "stats": {}}

    _last_load_time = now
    return _feedback_cache


def _save_feedback(data):
    """保存反馈历史"""
    global _feedback_cache, _last_load_time
    _ensure_dir()
    try:
        with open(_FEEDBACK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _feedback_cache = data
        _last_load_time = datetime.now().timestamp()
    except IOError as e:
        logger.error(f"保存反馈历史失败: {e}")


def record_query(user_query, intent_result, user_confirmed=False, corrected_endpoint=None,
                 corrected_fields=None):
    """
    记录一次查询及其结果

    Args:
        user_query: 用户原始查询
        intent_result: 解析结果 dict
        user_confirmed: 用户是否确认了结果正确
        corrected_endpoint: 如果用户修正了端点，填入修正后的端点路径
        corrected_fields: 如果用户修正了字段，填入修正后的字段列表
    """
    data = _load_feedback()

    record = {
        "query": user_query.strip(),
        "timestamp": datetime.now().isoformat(),
        "parsed_endpoint": intent_result.get("target_api", ""),
        "parsed_api_name": intent_result.get("api_name", ""),
        "parsed_fields": intent_result.get("fields", []),
        "parsed_confidence": intent_result.get("confidence", 0),
        "user_confirmed": user_confirmed,
        "corrected_endpoint": corrected_endpoint or "",
        "corrected_fields": corrected_fields or [],
        "source_type": intent_result.get("source_type", "api"),
    }

    data["records"].append(record)

    # 更新统计
    stats = data["stats"]
    endpoint_key = corrected_endpoint or intent_result.get("target_api", "")
    if endpoint_key:
        if endpoint_key not in stats:
            stats[endpoint_key] = {"total": 0, "confirmed": 0, "corrected_to": 0}
        stats[endpoint_key]["total"] += 1
        if user_confirmed:
            stats[endpoint_key]["confirmed"] += 1
        if corrected_endpoint:
            stats[endpoint_key]["corrected_to"] += 1

    # 限制历史记录数量（保留最近 1000 条）
    if len(data["records"]) > 1000:
        data["records"] = data["records"][-1000:]

    _save_feedback(data)
    logger.info(f"记录查询反馈: query='{user_query[:30]}...' confirmed={user_confirmed} corrected={bool(corrected_endpoint)}")


def _query_similarity(q1, q2):
    """计算两个查询的相似度（综合字符串相似度和关键词重叠）"""
    # 字符串相似度
    str_sim = SequenceMatcher(None, q1, q2).ratio()

    # 关键词重叠
    words1 = set(re.findall(r'[\u4e00-\u9fff]{2,}', q1))
    words2 = set(re.findall(r'[\u4e00-\u9fff]{2,}', q2))
    if words1 and words2:
        overlap = len(words1 & words2) / max(len(words1), len(words2))
    else:
        overlap = 0

    # 加权融合
    return str_sim * 0.5 + overlap * 0.5


def get_feedback_boost(user_query, candidate_endpoints):
    """
    根据历史反馈数据，为候选端点提供 boost 分数

    Args:
        user_query: 用户查询
        candidate_endpoints: 候选端点列表 [{"endpoint": ep_dict, "score": float, ...}]

    Returns:
        修改了 score 的候选列表（在原 score 基础上叠加 boost）；
        若学习到的正确端点不在候选中，则注入为高权重新候选，确保「学一次、永久对」。
    """
    data = _load_feedback()
    records = data.get("records", [])

    if not records:
        return candidate_endpoints

    # 找最相似的历史查询，且优先选用「用户修正/确认」过的记录，
    # 避免被 parse_intent 自动记录的（可能错配的）历史污染。
    best_sim = 0.0
    best_record = None
    best_sim_corrected = 0.0
    best_record_corrected = None
    best_sim_confirmed = 0.0
    best_record_confirmed = None
    for rec in records[-200:]:  # 只看最近 200 条
        sim = _query_similarity(user_query, rec["query"])
        if sim <= 0.6:
            continue
        if rec.get("corrected_endpoint"):
            if sim > best_sim_corrected:
                best_sim_corrected = sim
                best_record_corrected = rec
        elif rec.get("user_confirmed"):
            if sim > best_sim_confirmed:
                best_sim_confirmed = sim
                best_record_confirmed = rec
        if sim > best_sim:
            best_sim = sim
            best_record = rec

    # 优先级：用户修正 > 用户确认 > 最相似兜底（兜底记录通常无信号，不误学）
    if best_record_corrected:
        best_record = best_record_corrected
        best_sim = best_sim_corrected
    elif best_record_confirmed:
        best_record = best_record_confirmed
        best_sim = best_sim_confirmed

    # 如果相似度足够高（> 0.6），应用 boost
    if best_sim > 0.6 and best_record:
        # 用户确认过的端点 → 强 boost
        # 用户修正过的端点 → 使用修正后的端点
        target_endpoint = best_record.get("corrected_endpoint") or (
            best_record["parsed_endpoint"] if best_record.get("user_confirmed") else None
        )

        if target_endpoint:
            boost = best_sim * 0.3  # 最多 +0.3
            if best_record.get("user_confirmed"):
                boost += 0.1  # 用户确认过的额外加分

            # 1) 若目标端点已在候选中 → 直接加权
            injected = False
            for cand in candidate_endpoints:
                if cand["endpoint"]["path"] == target_endpoint:
                    cand["score"] += boost
                    cand["feedback_boost"] = round(boost, 3)
                    cand["feedback_source"] = f"历史相似查询(相似度={best_sim:.2f})"
                    logger.info(f"反馈 boost: {cand['endpoint']['name']} +{boost:.3f} (相似度={best_sim:.2f})")
                    injected = True
                    break

            # 2) 关键修复：目标端点在候选中不存在（被规则引擎漏掉/错配父级）时，
            #    把它作为新候选注入并给「压过规则匹配」的高权重，使学习结果真正胜出。
            if not injected:
                existing_max = max([c["score"] for c in candidate_endpoints], default=0.0)
                injected_score = max(existing_max, 1.0) + boost + 0.05
                ep = _lookup_endpoint(target_endpoint)
                new_cand = {
                    "endpoint": ep,
                    "score": round(injected_score, 3),
                    "matched_keywords": ["反馈学习"],
                    "match_type": "feedback_learned",
                    "feedback_boost": round(boost + 0.05, 3),
                    "feedback_source": f"历史相似查询(相似度={best_sim:.2f})",
                }
                if best_record.get("parsed_fields") and best_sim > 0.7:
                    new_cand["feedback_fields"] = best_record["parsed_fields"]
                candidate_endpoints.append(new_cand)
                logger.info(f"反馈注入新候选: {ep.get('name', target_endpoint)} score={injected_score:.3f} (相似度={best_sim:.2f})")

        # 如果历史记录中有字段信息，也返回供参考
        if best_record.get("parsed_fields") and best_sim > 0.7:
            for cand in candidate_endpoints:
                if not cand.get("feedback_fields"):
                    cand["feedback_fields"] = best_record["parsed_fields"]

    return candidate_endpoints


def _lookup_endpoint(path):
    """按路径查找真实端点元数据；找不到则构造最小可用 dict（path + 推导 name）。"""
    try:
        from kangyang.api_crawler import KNOWN_ENDPOINTS as _KEP
        for e in _KEP:
            if e.get("path") == path:
                return e
    except Exception:
        pass
    segs = [s for s in path.rstrip("/").split("/") if s and s != "dev-api"]
    name = segs[-2] if (len(segs) >= 2 and segs[-1] == "list") else segs[-1] if segs else path
    return {"path": path, "name": name or path, "module": segs[0] if segs else ""}


def get_endpoint_stats():
    """获取端点统计信息（用于前端展示学习进度）"""
    data = _load_feedback()
    stats = data.get("stats", {})
    total_queries = len(data.get("records", []))

    result = {
        "total_queries": total_queries,
        "endpoint_stats": [],
    }

    for path, stat in sorted(stats.items(), key=lambda x: -x[1]["total"]):
        result["endpoint_stats"].append({
            "endpoint": path,
            "total": stat["total"],
            "confirmed": stat["confirmed"],
            "corrected": stat["corrected_to"],
            "accuracy": round(stat["confirmed"] / max(stat["total"], 1), 2),
        })

    return result


def clear_feedback():
    """清空反馈历史"""
    global _feedback_cache
    _feedback_cache = {"records": [], "stats": {}}
    _save_feedback(_feedback_cache)
    logger.info("反馈历史已清空")
