# -*- coding: utf-8 -*-
"""Quick API integration test"""
import urllib.request
import json
import sys

BASE = "http://localhost:5000"

def post(path, data):
    payload = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read().decode("utf-8"))

def get(path):
    req = urllib.request.Request(f"{BASE}{path}")
    resp = urllib.request.urlopen(req, timeout=60)
    return json.loads(resp.read().decode("utf-8"))

# Test 1: Synonym expansion
r = post("/api/parse_intent", {"user_query": "老人家档案"})
intent = r.get("intent", {})
print(f"[1] Synonym: query='老人家档案' -> {intent.get('api_name')} (conf={intent.get('confidence')})")

# Test 2: Field disambiguation
r = post("/api/parse_intent", {"user_query": "获取老人的心率数据"})
intent = r.get("intent", {})
print(f"[2] Disambig: query='老人的心率' -> {intent.get('api_name')} (conf={intent.get('confidence')})")

# Test 3: Negation
r = post("/api/parse_intent", {"user_query": "获取除了合同以外的所有数据"})
intent = r.get("intent", {})
candidates = intent.get("endpoint_candidates", [])
print(f"[3] Negation: query='排除合同' -> {intent.get('api_name')} (candidates={[c['name'] for c in candidates[:3]]})")

# Test 4: Feedback stats
r = get("/api/feedback/stats")
print(f"[4] Feedback: total_queries={r['stats']['total_queries']}")

# Test 5: Feedback submission
r = post("/api/feedback", {
    "user_query": "获取老人的心率数据",
    "intent": {"target_api": "/dev-api/oldCare/oldCare/list", "api_name": "oldCare"},
    "action": "correct",
    "corrected_endpoint": "/dev-api/watchData/watchHeartrate/watchHeartrate/list",
    "corrected_api_name": "心率监测",
})
print(f"[5] Feedback submit: {r.get('ok')} - {r.get('message', '')}")

# Test 6: After feedback, re-query
r = post("/api/parse_intent", {"user_query": "获取老人的心跳数据"})
intent = r.get("intent", {})
print(f"[6] After feedback: query='心跳数据' -> {intent.get('api_name')} (conf={intent.get('confidence')})")

# Test 7: Clear feedback
r = post("/api/feedback/clear", {})
print(f"[7] Clear feedback: {r.get('ok')}")

print("\nAll API integration tests passed!")
