"""M4 Query 理解的单元测试（不碰网络：LLM 一律注入 fake）。"""

from __future__ import annotations

import pytest

from app.core.query_understanding import analyze, extract_error_codes, extract_resource


def test_pod_oom_exit_code():
    plan = analyze("Pod OOMKilled，exit code 137，内存超限被杀死", allow_llm=False)
    assert plan.resource_type == "pod"
    assert plan.sub_index == "k8s"
    assert "exit code 137" in plan.error_codes
    assert "OOMKilled" in plan.event_reasons
    assert plan.intent == "diagnose"
    assert plan.need_llm is False
    assert plan.llm_used is False
    assert plan.source == "rule"
    assert "exit code 137" in plan.search_text
    assert plan.language == "zh"


def test_nginx_502_worded():
    plan = analyze("nginx 返回 502 Bad Gateway，页面打不开", allow_llm=False)
    assert plan.resource_type == "nginx"
    assert plan.sub_index == "nginx"
    assert "502" in plan.error_codes
    assert plan.intent == "diagnose"


def test_mysql_errno_and_deadlock():
    plan = analyze("MySQL ERROR 1213 (40001): Deadlock found when trying to get lock", allow_llm=False)
    assert plan.resource_type == "mysql"
    assert plan.sub_index == "mysql"
    assert "1213" in plan.error_codes
    assert "Deadlock found" in plan.event_reasons


def test_nodes_unavailable():
    plan = analyze("0/3 nodes are available: 3 Insufficient cpu.", allow_llm=False)
    assert "0/3 nodes are available" in plan.error_codes
    assert plan.resource_type == "node"
    assert plan.sub_index == "k8s"


def test_redis_maxmemory():
    plan = analyze("Redis OOM command not allowed when used memory > maxmemory", allow_llm=False)
    assert plan.resource_type == "redis"
    assert plan.sub_index == "redis"
    assert "maxmemory" in plan.event_reasons
    assert plan.intent == "diagnose"


def test_empty_fields_for_short_unknown():
    plan = analyze("怎么办", allow_llm=False)
    assert plan.rule_hit is False
    assert plan.need_llm is False  # 太短，不值得调 LLM
    assert plan.intent in ("unknown", "diagnose", "howto")  # "怎么办" 命中 howto 正则，也不该调 LLM
    assert plan.source == "rule"


def test_long_generic_marks_need_llm_but_skips_when_disabled():
    plan = analyze("服务突然变得很慢，页面加载很久也没有任何提示信息出来", allow_llm=False)
    assert plan.need_llm is True
    assert plan.llm_used is False
    assert plan.source == "rule"


def test_llm_success_merges_fields():
    def fake_llm(_plan):
        return {
            "intent": "diagnose",
            "resource_type": "redis",
            "event_reasons": ["Evicted"],
            "keywords": ["超时", "上游"],
        }

    plan = analyze("服务突然变得很慢，页面加载很久也没有任何提示信息出来", llm=fake_llm, allow_llm=True)
    assert plan.llm_used is True
    assert plan.source == "rule+llm"
    assert plan.resource_type == "redis"
    assert plan.sub_index == "redis"
    assert "Evicted" in plan.event_reasons
    assert "超时" in plan.keywords
    assert plan.llm_latency_ms is not None


def test_llm_failure_degrades():
    def boom(_plan):
        raise RuntimeError("connection timeout")

    plan = analyze("服务突然变得很慢，页面加载很久也没有任何提示信息出来", llm=boom, allow_llm=True)
    assert plan.llm_used is False
    assert plan.source == "rule+llm_failed"
    assert "RuntimeError" in (plan.llm_error or "")
    assert plan.llm_latency_ms is not None


def test_llm_bad_payload_degrades():
    plan = analyze("服务突然变得很慢，页面加载很久也没有任何提示信息出来", llm=lambda _p: ["nope"], allow_llm=True)
    assert plan.llm_used is False
    assert plan.source == "rule+llm_failed"
    assert plan.llm_error


def test_weak_event_not_duplicated_into_search_text():
    plan = analyze("Error: 容器启动失败", allow_llm=False)
    assert "Error" in plan.event_reasons
    # 只出现一次（来自原文），没被再拼一遍
    assert plan.search_text.count("Error") == 1


def test_howto_intent():
    plan = analyze("如何配置 PVC 的 storageClassName", allow_llm=False)
    assert plan.intent == "howto"
    assert plan.resource_type == "pvc"
    assert plan.sub_index == "k8s"
    assert plan.need_llm is False


def test_language_detection():
    assert analyze("Pod OOMKilled exit code 137", allow_llm=False).language == "en"
    assert analyze("容器重启", allow_llm=False).language == "zh"


def test_keywords_not_empty():
    plan = analyze("容器被 OOMKilled 后一直重启", allow_llm=False)
    assert plan.keywords
    assert all(len(k) >= 2 for k in plan.keywords)


def test_error_codes_dedup():
    assert extract_error_codes("exit code 137 ... exit code 137") == ["exit code 137"]


def test_resource_priority_container_group():
    # 容器组 必须比 容器 先判定，否则会被判成 container
    assert extract_resource("容器组调度失败")[0] == "pod"


@pytest.mark.parametrize("text", ["", "   "])
def test_empty_text_is_safe(text):
    plan = analyze(text, allow_llm=False)
    assert plan.clean == ""
    assert plan.search_text == ""
    assert plan.source == "rule"

def test_resource_first_occurrence_wins():
    # 主语先出现：节点在前 -> node；DaemonSet 在前 -> daemonset
    # （按词典顺序会分别被 container / node 抢先命中，D3 探针实测过）
    assert extract_resource("worker 节点变成 NodeNotReady，上面跑的容器全挂了")[0] == "node"
    assert extract_resource("DaemonSet 没有在每个节点上被创建")[0] == "daemonset"


def test_resource_same_position_prefers_longer_alias():
    # 同一位置（"容器组" 与 "容器" 都从 index 0 开始）时按词典顺序取长的
    assert extract_resource("容器组 里的容器反复重启")[0] == "pod"


def test_resource_none_when_no_keyword():
    assert extract_resource("服务突然变慢，页面加载很久")[0] is None

