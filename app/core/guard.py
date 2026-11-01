"""引用校验与拒答（M8）：ADR-4「引用强约束 + 后置校验」与 ADR-5「双保险」的落地。

为什么 allowed 要由调用方显式传进来，而不是从 chunks 反推：校验侧唯一的判据是
"这次真正喂给模型的上下文是哪几条"。生成侧只看得到那几条块，任何不在这份白名单里的
chunk_id 都是模型编出来的（或张冠李戴到上一轮的块上）。把上下文边界钉成入参，
校验逻辑就只剩一次集合查询，不会因为上游改了取数逻辑而悄悄放过幻觉引用。

口径（与 CitationReport 字段一一对应）：

- total：引用点总数 = Σ causes[].evidence 条数 + citations 条数（按出现次数算，
  同一 id 出现两次就是两个引用点）；
- valid：其中 chunk_id ∈ allowed 的引用点条数；
- invalid_ids：不在 allowed 里的 chunk_id（去重，保留首次出现顺序）。空串也算无效
  ——一条没有 chunk_id 的引用同样是幻觉引用，只是没有 id 可指，按 "" 记一条；
- orphan_causes：没有任何"有效 evidence"的 cause 下标（没 evidence、evidence 全无效都算）；
- coverage：至少有一条有效 evidence 的 cause 占比（coverage 是给评测看的"有据可依率"，
  没有 cause 时记 0.0；abstained 的空卡不计入分母）；
- ok（CitationReport 的属性）：invalid_ids 与 orphan_causes 都为空。ok=False 时按设计
  清单走"重生成一次再降级"，这里只出结论，不做重试。

should_abstain 的口径（§5 M8 + §6.5 第 2 条）：只看**最高分**，不看平均分、不看条数。
理由：拒答要挡的是"整个上下文都跟问题无关"，只要有一条真相关，就该让模型去答；
用平均分会被"1 条相关 + 19 条无关"稀释成拒答。

  1. hits 为空 -> True（没检索到东西，无话可说）；
  2. 否则取 max(rerank_score ?? score)，**严格小于** threshold 才拒答（等于阈值算通过，
     阈值是"及格线"不是"及格线以上"）；
  3. 降级时 rerank_score 为 None，会退回 RRF 分——RRF 分是 1/61 量级，必然触发拒答。
     所以 API 层必须先看 chunk.degraded：degraded=True 的那次不走拒答判定，
     交给生成侧的兜底（这是 ADR-5 双保险的本意，否则重排一挂就是全量拒答）。
"""

from __future__ import annotations

from app.core.schemas import CitationReport, DiagnosisCard, ScoredChunk


def best_score(hits: list[ScoredChunk]) -> float:
    """取最高分：有 rerank_score 用 rerank_score，否则退回 RRF score。

    单独抽出来是为了让"回退规则"只有一处实现：should_abstain 用它判定，
    API 层用它写 query_log / metrics，口径不会分叉。
    """
    if not hits:
        return 0.0
    return max(_hit_score(chunk) for chunk in hits)


def _hit_score(chunk: ScoredChunk) -> float:
    if chunk.rerank_score is not None:
        return float(chunk.rerank_score)
    return float(chunk.score or 0.0)


def should_abstain(hits: list[ScoredChunk], threshold: float) -> bool:
    """检索侧拒答判定：Top1 低于阈值就拒答。阈值由调用方传 settings.abstain_threshold。"""
    if not hits:
        return True
    return best_score(hits) < float(threshold)


def verify_citations(card: DiagnosisCard, allowed: set[str]) -> CitationReport:
    """逐条核对卡片引用是否落在本次上下文白名单里。只读，不改 card。"""
    report = CitationReport()
    if card is None:  # 防御性：生成侧降级时可能拿不到卡
        return report

    allowed_ids = set(allowed or ())
    invalid_ids: list[str] = []
    seen_invalid: set[str] = set()
    causes = list(card.causes or [])

    total = 0
    valid = 0
    supported_causes = 0
    orphan_causes: list[int] = []

    for index, cause in enumerate(causes):
        cause_supported = False
        for evidence in cause.evidence or []:
            total += 1
            chunk_id = evidence.chunk_id or ""
            if chunk_id and chunk_id in allowed_ids:
                valid += 1
                cause_supported = True
                continue
            if chunk_id not in seen_invalid:
                seen_invalid.add(chunk_id)
                invalid_ids.append(chunk_id)
        if cause_supported:
            supported_causes += 1
        else:
            orphan_causes.append(index)

    for citation in card.citations or []:
        total += 1
        chunk_id = citation.chunk_id or ""
        if chunk_id and chunk_id in allowed_ids:
            valid += 1
            continue
        if chunk_id not in seen_invalid:
            seen_invalid.add(chunk_id)
            invalid_ids.append(chunk_id)

    report.total = total
    report.valid = valid
    report.invalid_ids = invalid_ids
    report.orphan_causes = orphan_causes
    report.coverage = round(supported_causes / len(causes), 4) if causes else 0.0
    return report
