"""M7 生成的单元测试：LLM 客户端一律 monkeypatch 成假实现，不联网、不落盘。

覆盖的四条主路径（清单 §5 M7 验收）：
正常 JSON / JSON 坏后重试成功 / 两次都坏降级 / 模型主动 abstain；
另外覆盖引用后置校验、只读命令过滤、上下文预算、空输入与异常路径。
"""

from __future__ import annotations

import json

import pytest

from app.config import Settings
from app.core import generator, prompts
from app.core.schemas import DiagnosisCard, ScoredChunk

QUERY = "Pod 被 OOMKilled，exit code 137，反复重启"


def _chunk(chunk_id, text, *, heading="", url="", title="", score=0.5):
    return ScoredChunk(
        chunk_id=chunk_id,
        doc_id="doc-" + chunk_id,
        text=text,
        heading_path=heading,
        url=url,
        title=title,
        score=score,
    )


@pytest.fixture()
def chunks():
    return [
        _chunk(
            "k8s-oom-001",
            "OOMKilled 表示容器内存超过 limit 被内核 OOM killer 杀死，退出码 137 就是 SIGKILL。",
            heading="Pod 异常 > OOMKilled",
            url="https://example.com/a",
            title="Pod 异常排查",
            score=0.91,
        ),
        _chunk(
            "k8s-oom-002",
            "先看 kubectl describe pod 的 Last State 与 Events，再核对 resources.limits.memory。",
            heading="Pod 异常 > 排查步骤",
            url="https://example.com/b",
            title="Pod 异常排查",
            score=0.83,
        ),
        _chunk(
            "k8s-oom-003",
            "JVM 堆上限超过容器 limit 时也会被 OOMKilled，需要设置 -XX:MaxRAMPercentage。",
            heading="JVM > 容器内存",
            url="https://example.com/c",
            title="JVM 内存",
            score=0.77,
        ),
    ]


@pytest.fixture()
def settings():
    return Settings(llm_max_retry=1, deepseek_api_key="test-key")


class FakeLLM:
    """按调用顺序返回预设输出：字符串自动包成 LLMResponse，异常原样抛出。"""

    def __init__(self, *responses, prompt_tokens=1200, completion_tokens=260, finish_reason=""):
        self.responses = list(responses)
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.finish_reason = finish_reason
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, messages, *, timeout=None):
        self.calls.append(list(messages))
        item = self.responses[min(len(self.calls), len(self.responses)) - 1]
        if isinstance(item, Exception):
            raise item
        if isinstance(item, generator.LLMResponse):
            return item
        return generator.LLMResponse(
            item,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            model="deepseek-flash",
            finish_reason=self.finish_reason,
        )


def _install(monkeypatch, fake):
    monkeypatch.setattr(generator, "default_llm_callable", lambda settings=None: fake)
    return fake


def _good_payload():
    return {
        "abstained": False,
        "summary": "容器内存超过 limit 被 OOM killer 杀死（exit code 137）。",
        "causes": [
            {
                "title": "容器内存超过 limit",
                "confidence": 0.82,
                "explanation": "事件里出现 OOMKilled 且退出码为 137。",
                "evidence": [{"chunk_id": "k8s-oom-001"}],
            }
        ],
        "verify_commands": [
            "kubectl describe pod <pod> -n <namespace>",
            "kubectl logs <pod> -c <container> --tail=100",
        ],
        "actions": ["调大 resources.limits.memory"],
        "citations": [
            {"chunk_id": "k8s-oom-001"},
            {"chunk_id": "k8s-oom-002"},
            {"chunk_id": "k8s-oom-001"},
        ],
    }


def test_normal_card(monkeypatch, chunks, settings):
    fake = _install(monkeypatch, FakeLLM(json.dumps(_good_payload(), ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert isinstance(card, DiagnosisCard)
    assert card.abstained is False and card.degraded is False
    assert card.summary.startswith("容器内存超过 limit")
    assert [c.title for c in card.causes] == ["容器内存超过 limit"]
    assert card.causes[0].confidence == pytest.approx(0.82)
    # evidence 缺 url 时用块元数据补全，score 缺省取块的分数
    assert card.causes[0].evidence[0].url == "https://example.com/a"
    assert card.causes[0].evidence[0].score == pytest.approx(0.91)
    # citations 去重、补 title/heading_path/url
    assert [c.chunk_id for c in card.citations] == ["k8s-oom-001", "k8s-oom-002"]
    assert card.citations[1].url == "https://example.com/b"
    assert card.citations[1].heading_path == "Pod 异常 > 排查步骤"
    assert card.verify_commands == [
        "kubectl describe pod <pod> -n <namespace>",
        "kubectl logs <pod> -c <container> --tail=100",
    ]
    assert card.prompt_tokens == 1200 and card.completion_tokens == 260
    assert card.model == "deepseek-flash"
    assert card.cost_cny == pytest.approx((1200 * 2.0 + 260 * 8.0) / 1_000_000)
    assert card.diagnosis_id.startswith("dg_")
    assert card.latency_ms >= 0
    assert len(fake.calls) == 1
    # system prompt 规则与候选块编号都真的发给了模型
    assert fake.calls[0][0]["role"] == "system"
    assert prompts.SYSTEM_PROMPT in fake.calls[0][0]["content"]
    assert "[k8s-oom-001]" in fake.calls[0][1]["content"]


def test_retry_after_bad_json(monkeypatch, chunks, settings):
    fake = _install(
        monkeypatch,
        FakeLLM("好的，我分析一下：这不是 JSON。", json.dumps(_good_payload(), ensure_ascii=False)),
    )
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(fake.calls) == 2
    assert card.degraded is False and card.causes
    # 重试时把上次原文 + 纠错指令一起发回，而不是原样重发
    retry_messages = fake.calls[1]
    assert retry_messages[-1]["content"] == prompts.RETRY_HINT
    assert retry_messages[-2]["role"] == "assistant"
    assert retry_messages[-2]["content"].startswith("好的，我分析一下")


def test_degrade_after_two_bad_json(monkeypatch, chunks, settings):
    fake = _install(monkeypatch, FakeLLM("不是 JSON", "[1, 2, 3]"))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(fake.calls) == 2
    assert card.degraded is True
    assert card.abstained is False
    assert card.causes == []
    assert card.verify_commands == []
    # 降级摘要只摆原文片段，不做推理
    assert "已降级为原文片段" in card.summary
    assert "[k8s-oom-001]" in card.summary
    allowed = {c.chunk_id for c in chunks}
    assert {c.chunk_id for c in card.citations} <= allowed
    assert card.abort_reason


def test_model_abstains(monkeypatch, chunks, settings):
    payload = {
        "abstained": True,
        "summary": "资料里没有覆盖这次出现的 CNI 报错。",
        "causes": [{"title": "模型多给的原因", "evidence": [{"chunk_id": "k8s-oom-001"}]}],
        "verify_commands": ["kubectl get pods"],
        "citations": [{"chunk_id": "k8s-oom-001"}],
    }
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert card.abstained is True
    assert card.causes == []  # abstained 时 causes 必须为空
    assert card.verify_commands == [] and card.actions == []
    assert card.abort_reason == "insufficient_evidence"
    assert card.summary == "资料里没有覆盖这次出现的 CNI 报错。"
    assert card.citations[0].chunk_id == "k8s-oom-001"


def test_fabricated_chunk_ids_become_abstain(monkeypatch, chunks, settings):
    payload = {
        "abstained": False,
        "summary": "是内核参数 vm.swappiness 导致的。",
        "causes": [
            {
                "title": "vm.swappiness 配置过低",
                "confidence": 0.9,
                "evidence": [{"chunk_id": "k8s-does-not-exist"}],
            }
        ],
        "verify_commands": ["kubectl get pods"],
        "citations": [{"chunk_id": "k8s-does-not-exist"}],
    }
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert card.abstained is True
    assert card.abort_reason == "citations_invalid"
    assert card.causes == []
    assert "不编造原因" in card.summary
    allowed = {c.chunk_id for c in chunks}
    assert {c.chunk_id for c in card.citations} <= allowed


def test_verify_commands_stay_read_only(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["verify_commands"] = [
        "kubectl get pods -o wide",
        "kubectl delete pod <pod> -n <namespace>",
        "rm -rf /data",
        "kubectl get pods > /tmp/pods.txt",
        "kubectl get pods && kubectl delete pod <pod>",
        "cat /var/log/messages | grep -i oom",
    ]
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert card.verify_commands == [
        "kubectl get pods -o wide",
        "cat /var/log/messages | grep -i oom",
    ]


def test_verify_commands_capped(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["verify_commands"] = ["kubectl get pods", "kubectl get nodes", "df -h", "free -m", "uptime", "kubectl top pod", "ps aux"]
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(card.verify_commands) == generator.MAX_VERIFY_COMMANDS


def test_context_budget_keeps_every_id():
    many = [_chunk("c-%02d" % i, "内存超限被 OOM killer 杀死，退出码 137。" * 60, heading="很长的标题" * 8) for i in range(12)]
    text, used = generator.build_context(many, budget=2500)

    assert used and len(used) <= generator.MAX_CONTEXT_CHUNKS
    assert len(text) <= 2500
    for chunk in used:
        assert "[" + chunk.chunk_id + "]" in text
    # 只给被用到的块，调用方拿到的就是"允许引用的集合"
    assert len(used) == text.count("[c-")


def test_user_message_within_budget(monkeypatch, settings):
    fake = _install(monkeypatch, FakeLLM(json.dumps({"abstained": True, "summary": "资料不足"}, ensure_ascii=False)))
    many = [_chunk("c-%02d" % i, "内存超限被 OOM killer 杀死，退出码 137。" * 60, heading="很长的标题" * 8) for i in range(12)]
    generator.generate(QUERY, many, settings=settings)

    user_message = fake.calls[0][1]["content"]
    assert generator.PROMPT_CHAR_BUDGET == 2600
    assert len(user_message) <= 2600
    assert "[c-00]" in user_message
    # 块数也受控：最多 5 块，剩下的预算留给正文而不是让标题把预算吃光
    assert user_message.count("[c-") <= generator.MAX_CONTEXT_CHUNKS


def test_empty_chunks_skips_llm(monkeypatch, settings):
    fake = _install(monkeypatch, FakeLLM("不该被调用"))
    card = generator.generate(QUERY, [], settings=settings)

    assert fake.calls == []
    assert card.abstained is True
    assert card.causes == [] and card.verify_commands == []
    assert card.abort_reason == "no_chunks"


def test_llm_exception_degrades_without_retry(monkeypatch, chunks, settings):
    fake = _install(monkeypatch, FakeLLM(RuntimeError("connection reset")))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(fake.calls) == 1  # 网络错误重试没有意义
    assert card.degraded is True
    assert card.causes == []
    assert "RuntimeError" in card.abort_reason
    assert card.prompt_tokens == 0


def test_confidence_clamped_and_service_fields_ignored(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["causes"][0]["confidence"] = 1.7
    payload["diagnosis_id"] = "dg_hacked"
    payload["latency_ms"] = 999999
    payload["cost_cny"] = 9.9
    payload["prompt_tokens"] = 88888
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert card.causes[0].confidence == 1.0
    assert card.diagnosis_id != "dg_hacked" and card.diagnosis_id.startswith("dg_")
    assert card.latency_ms < 60000
    assert card.cost_cny < 1.0
    assert card.prompt_tokens == 1200


@pytest.mark.parametrize(
    "command",
    [
        "kubectl get pods -o wide",
        "kubectl describe pod <pod> -n <namespace>",
        "kubectl logs <pod> -c app --tail=100",
        "kubectl top pod",
        "kubectl get events --sort-by=.lastTimestamp",
        "kubectl rollout status deployment/web",
        "kubectl config view",
        "cat /var/log/messages | grep -i oom",
        "journalctl -u kubelet --since '1 hour ago'",
        "df -h && free -m",
        "systemctl status kubelet",
    ],
)
def test_read_only_commands_allowed(command):
    assert generator.is_read_only_command(command) is True


@pytest.mark.parametrize(
    "command",
    [
        "kubectl delete pod <pod> -n <namespace>",
        "kubectl apply -f deploy.yaml",
        "kubectl rollout restart deployment/web",
        "kubectl rollout undo deployment/web",
        "kubectl config set-context prod",
        "kubectl exec -it <pod> -- sh",
        "kubectl get pods && kubectl delete pod <pod>",
        "kubectl get pods; kubectl delete pod <pod>",
        "kubectl get pods > /tmp/pods.txt",
        "rm -rf /data",
        "systemctl restart kubelet",
        "echo $(rm -rf /)",
        "",
    ],
)
def test_write_commands_rejected(command):
    assert generator.is_read_only_command(command) is False

# ------------------------------------------------ 输出预算：模型不听话时的兜底


def test_causes_capped_to_two(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["causes"] = [
        {"title": "原因一", "confidence": 0.9, "explanation": "依据一", "evidence": [{"chunk_id": "k8s-oom-001"}]},
        {"title": "原因二", "confidence": 0.7, "explanation": "依据二", "evidence": [{"chunk_id": "k8s-oom-002"}]},
        {"title": "原因三", "confidence": 0.6, "explanation": "依据三", "evidence": [{"chunk_id": "k8s-oom-003"}]},
    ]
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert [c.title for c in card.causes] == ["原因一", "原因二"]


def test_evidence_capped_per_cause(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["causes"][0]["evidence"] = [
        {"chunk_id": "k8s-oom-001"},
        {"chunk_id": "k8s-oom-002"},
        {"chunk_id": "k8s-oom-003"},
    ]
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert [e.chunk_id for e in card.causes[0].evidence] == ["k8s-oom-001", "k8s-oom-002"]


def test_actions_capped_and_clipped(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["actions"] = ["检查内存" * 20, "调大 limit" * 10, "加监控" * 10, "扩容" * 10, "回滚" * 10]
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(card.actions) == generator.MAX_ACTIONS
    for action in card.actions:
        assert len(action) <= generator.ACTION_MAX_CHARS
    assert card.actions[0].endswith("…")


def test_summary_clipped_to_100(monkeypatch, chunks, settings):
    payload = _good_payload()
    payload["summary"] = "内存超限" * 60
    _install(monkeypatch, FakeLLM(json.dumps(payload, ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(card.summary) <= generator.SUMMARY_MAX_CHARS
    assert card.summary.endswith("…")


def test_length_truncation_retries_with_shorter_hint(monkeypatch, chunks, settings):
    truncated = generator.LLMResponse(
        '{"abstained": false, "summary": "容器内存超限',
        prompt_tokens=900,
        completion_tokens=800,
        model="deepseek-flash",
        finish_reason="length",
    )
    fake = _install(monkeypatch, FakeLLM(truncated, json.dumps(_good_payload(), ensure_ascii=False)))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(fake.calls) == 2
    # 长度截断的重试提示与"格式错误"不同：要求砍条数/字数，而不是重排版
    assert fake.calls[1][-1]["content"] == prompts.LENGTH_RETRY_HINT
    assert card.degraded is False and card.causes
    assert card.completion_tokens == 800 + 260


def test_length_truncation_twice_degrades(monkeypatch, chunks, settings):
    truncated = generator.LLMResponse(
        '{"abstained": false, "summary": "容器内存超限',
        prompt_tokens=900,
        completion_tokens=800,
        model="deepseek-flash",
        finish_reason="length",
    )
    fake = _install(monkeypatch, FakeLLM(truncated, truncated))
    card = generator.generate(QUERY, chunks, settings=settings)

    assert len(fake.calls) == 2
    assert card.degraded is True
    assert card.causes == []
    assert "length" in card.abort_reason
    assert "[k8s-oom-001]" in card.summary

