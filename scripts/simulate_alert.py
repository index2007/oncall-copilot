"""打一条 Alertmanager v4 webhook 到 /webhook/alertmanager，用来演示"告警自动变诊断卡"。

为什么单独写这个脚本：D5 的七接口里只有 webhook 没法用 curl 一句话演示——真 payload
有 15 个字段，手打必然错一个。这里内置 5 个真实故障场景（K8s OOM、nginx 502、
MySQL 死锁、Redis 内存打满、节点 NotReady），字段与 Alertmanager v4 对齐，
--dry-run 只打印不发送。

用法：
    python scripts/simulate_alert.py --dry-run                 # 看 5 条 payload
    python scripts/simulate_alert.py                           # 依次发给本机服务
    python scripts/simulate_alert.py --scenario oom --json     # 只发一条，输出 JSON
    python scripts/simulate_alert.py --url http://127.0.0.1:8000 --scenario nginx502
"""

from __future__ import annotations

import argparse
import json
import sys
import zlib
from datetime import datetime, timezone
from typing import Any

import httpx

DEFAULT_URL = "http://127.0.0.1:8000"

# 每个场景：labels 决定 Alertmanager 的告警名/严重级别/对象，annotations 的
# description 就是喂给诊断服务的自然语言症状（webhook 侧优先取 description）。
SCENARIOS: dict[str, dict[str, Any]] = {
    "oom": {
        "alertname": "KubePodOOMKilled",
        "severity": "critical",
        "namespace": "prod",
        "pod": "checkout-api-7d9f8c6b5-x2k4p",
        "description": (
            "Pod checkout-api-7d9f8c6b5-x2k4p 反复重启，kubectl describe 显示 "
            "Last State: Terminated, Reason: OOMKilled, Exit Code: 137，"
            "容器内存 limit 是 512Mi，监控里 RSS 峰值到 498Mi。"
        ),
        "summary": "checkout-api 容器被 OOMKilled",
    },
    "nginx502": {
        "alertname": "NginxUpstream5xx",
        "severity": "critical",
        "instance": "nginx-ingress-1",
        "description": (
            "nginx error.log 持续出现 upstream prematurely closed connection while "
            "reading response header from upstream, client: 10.0.0.12, "
            "upstream: http://10.0.1.20:8080/order, 502 比例 12%。"
        ),
        "summary": "网关 502 比例上升",
    },
    "mysql-deadlock": {
        "alertname": "MySQLDeadlockDetected",
        "severity": "warning",
        "instance": "mysql-prod-01",
        "description": (
            "应用日志报 Error 1213: Deadlock found when trying to get lock; "
            "try restarting transaction，集中在 orders 表的 UPDATE 与另一条 "
            "SELECT ... FOR UPDATE 之间，每分钟 3-5 次。"
        ),
        "summary": "MySQL 死锁频发",
    },
    "redis-memory": {
        "alertname": "RedisUsedMemoryHigh",
        "severity": "warning",
        "instance": "redis-cache-02",
        "description": (
            "Redis 写入报 OOM command not allowed when used memory > 'maxmemory'，"
            "INFO memory 显示 used_memory 3.9G / maxmemory 4G，maxmemory-policy 是没设。"
        ),
        "summary": "缓存写入被拒（内存打满）",
    },
    "node-notready": {
        "alertname": "KubeNodeNotReady",
        "severity": "critical",
        "node": "worker-3",
        "description": (
            "节点 worker-3 状态变为 NotReady，节点上 12 个 Pod 全部 Evicted，"
            "kubelet 日志有 node has insufficient memory 与 container runtime is down。"
        ),
        "summary": "worker-3 掉线",
    },
}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_payload(scenario: str, *, base_url: str = "") -> dict[str, Any]:
    """按 Alertmanager v4 的形状拼一条 payload（字段名与官方一致，多一个都别改）。"""
    spec = SCENARIOS[scenario]
    labels = {
        "alertname": spec["alertname"],
        "severity": spec["severity"],
    }
    for key in ("namespace", "pod", "instance", "node"):
        if spec.get(key):
            labels[key] = spec[key]
    annotations = {"summary": spec["summary"], "description": spec["description"]}
    return {
        "version": "4",
        "groupKey": '{}/{}'.format(spec["alertname"], labels.get("pod") or labels.get("instance") or labels.get("node") or "-"),
        "truncatedAlerts": 0,
        "status": "firing",
        "receiver": "oncall-copilot",
        "groupLabels": {"alertname": spec["alertname"]},
        "commonLabels": labels,
        "commonAnnotations": annotations,
        "externalURL": base_url or DEFAULT_URL,
        "alerts": [
            {
                "status": "firing",
                "labels": labels,
                "annotations": annotations,
                "startsAt": _now(),
                "endsAt": "0001-01-01T00:00:00Z",
                "generatorURL": "http://prometheus:9090/graph",
                # 用 crc32 而不是 hash()：hash() 每个进程加盐，两次运行 fingerprint 不同
                "fingerprint": "{:016x}".format(zlib.crc32(scenario.encode())),
            }
        ],
    }


def send(url: str, payload: dict[str, Any], timeout: float = 90.0) -> dict[str, Any]:
    with httpx.Client(timeout=timeout) as client:
        resp = client.post(url.rstrip("/") + "/webhook/alertmanager", json=payload)
    resp.raise_for_status()
    return resp.json()


def _print_result(name: str, body: dict[str, Any]) -> None:
    print("[{}] accepted={} skipped={} latency={} ms".format(
        name, body.get("accepted"), body.get("skipped"), body.get("latency_ms")))
    for item in body.get("results") or []:
        if item.get("error"):
            print("    {} -> error: {}".format(item.get("alertname"), item.get("error")))
            continue
        causes = item.get("causes") or []
        print("    {} -> {} | {}".format(
            item.get("alertname"),
            "拒答" if item.get("abstained") else "诊断",
            "；".join(causes) if causes else (item.get("summary") or "")[:60],
        ))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="模拟 Alertmanager v4 告警")
    parser.add_argument("--url", default=DEFAULT_URL, help="服务根地址，默认 %(default)s")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", *SCENARIOS],
        help="发哪个场景，默认全部",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印 payload，不发请求")
    parser.add_argument("--json", action="store_true", help="机器可读输出")
    args = parser.parse_args(argv)

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    payloads = {name: build_payload(name, base_url=args.url) for name in names}

    if args.dry_run:
        if args.json:
            print(json.dumps(payloads, ensure_ascii=False, indent=2))
        else:
            for name, payload in payloads.items():
                print("=== {} ({})".format(name, payload["alerts"][0]["labels"]["alertname"]))
                print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    failures = 0
    for name, payload in payloads.items():
        try:
            body = send(args.url, payload)
        except Exception as exc:  # noqa: BLE001 - 演示脚本，失败就报一行
            print("[{}] 请求失败：{}: {}".format(name, type(exc).__name__, exc))
            failures += 1
            continue
        if args.json:
            print(json.dumps({"scenario": name, "response": body}, ensure_ascii=False))
        else:
            _print_result(name, body)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
