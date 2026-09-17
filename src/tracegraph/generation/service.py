from tracegraph.core.contracts import (
    DEFAULT_MAX_HOPS,
    DEFAULT_WORKSPACE_ID,
    Answer,
    AnswerStatus,
    Claim,
    Evidence,
)
from tracegraph.core.ports import DomainAdapter, GraphRepository, Retriever
from tracegraph.generation.models import ModelRegistry, single_generator_registry
from tracegraph.generation.providers import (
    AnswerGenerator,
    ExtractiveAnswerGenerator,
    GeneratedAnswer,
    GenerationError,
    describe_path,
)


# 回答正文只由已通过校验的直接事实主张拼装。推导关联有独立字段
# （`Answer.derived_associations`），绝不再拼进正文 —— 否则同一批推导
# 会同时出现在正文和推导区块里，被读成两批不同的结论。
_ANSWER_HEADER = "根据当前知识库中的原文事实："
_ANSWER_FOOTER = "以上内容仅来自知识库原文摘录，用于知识检索与学习，不替代专业判断。"

# 同源直接冲突的判定规则：同一主体对同一客体同时给出这两种关系。
_OPPOSING_RELATION_TYPES = ("SHOULD_EAT", "SHOULD_NOT_EAT")

# 生成失败的对外文案；具体原因只以 error_code 形式给出，不泄露上游细节。
_GENERATION_FAILURE_TEXT = "答案生成服务暂时不可用，请稍后重试。"


class AnswerService:
    """在不依赖外部 LLM 的情况下生成有证据约束的结构化回答。

    `fallback_generator` 只在显式配置 `TRACEGRAPH_LLM_FALLBACK=extractive`
    时传入。降级绝不自作主张发生，且一旦发生就会被记进 metrics 与 warnings。
    """

    def __init__(
        self,
        retriever: Retriever,
        domain: DomainAdapter,
        generator: AnswerGenerator | None = None,
        graph_repository: GraphRepository | None = None,
        fallback_generator: AnswerGenerator | None = None,
        registry: ModelRegistry | None = None,
        workspace_id: str = DEFAULT_WORKSPACE_ID,
    ) -> None:
        self.retriever = retriever
        self.domain = domain
        self.graph = graph_repository
        self.fallback_generator = fallback_generator
        # 本次回答所在的知识库；检索按它限定范围。
        self.workspace_id = workspace_id
        # 默认生成器就是注册表的默认条目 —— 两者不可能指向不同的东西。
        self.registry = registry or single_generator_registry(
            generator or ExtractiveAnswerGenerator()
        )
        self.generator = self.registry.default_generator

    def for_workspace(
        self,
        *,
        domain: DomainAdapter,
        retriever: Retriever,
        graph_repository: GraphRepository | None,
        workspace_id: str,
    ) -> "AnswerService":
        """派生一个只服务本次请求的实例。

        注册表与降级生成器原样带过去，因此「当前用哪个模型」不随 Workspace
        改变；domain / retriever / graph_repository / workspace_id 则每个请求
        各持一份，装配时那个实例的 domain 永远不被改写，也不存在两个并发
        请求互相看到对方适配器的窗口。
        """
        return AnswerService(
            retriever,
            domain,
            graph_repository=graph_repository,
            fallback_generator=self.fallback_generator,
            registry=self.registry,
            workspace_id=workspace_id,
        )

    def answer(
        self,
        question: str,
        limit: int = 5,
        max_hops: int = DEFAULT_MAX_HOPS,
        generator_id: str | None = None,
    ) -> Answer:
        # 模型选择是「每次请求」的事：解析结果只活在本次调用里，没有会被
        # 并发请求互相覆盖的进程级状态。未知或不可用的模型在这里抛出，
        # 由 API 层翻译成 400 / 503。
        generator = (
            self.registry.default_generator
            if generator_id is None
            else self.registry.resolve(generator_id)
        )
        # 失败时报告「本来要用哪一个」，成功时报告「真正用了哪一个」，
        # 两者都是清单里的 ID —— 界面按 ID 反查显示名称。
        requested_id = generator_id or self.registry.default_id
        normalized = self.domain.normalize_question(question)
        if not normalized:
            raise ValueError("question 不能为空")

        preflight = self.domain.preflight_status(normalized)
        if preflight is not None:
            return Answer(
                status=preflight,
                text=self.domain.status_message(preflight, normalized),
            )

        retrieved = self.retriever.retrieve(
            normalized, limit, max_hops, self.workspace_id
        )
        # 原文事实（1 跳或无图路径）与推导关联（多跳）在此彻底分流：
        # 生成器只拿到 facts，因此 Claim 在数据流上不可能来自多跳推测。
        facts, derived = _split_by_hops(retrieved)
        # 下限只能是相对的。混合检索给的是 RRF 融合分，量纲由各检索器的权重
        # 决定：只被关键词命中的证据即使排第一也只有 1/(4+1)=0.2，任何绝对
        # 常量都会把整条关键词通路挡在门外 —— 通过网页上传的普通文档正好
        # 只走这条路，于是永远答不出来。
        minimum_score = facts[0].retrieval_score * 0.75 if facts else 0.0
        facts = tuple(
            evidence for evidence in facts if evidence.retrieval_score >= minimum_score
        )
        if not facts:
            # 没有原文事实就不作答，即使有多跳关联 —— 不做跨节点推测。
            status = AnswerStatus.INSUFFICIENT_EVIDENCE
            return Answer(
                status=status,
                text=self.domain.status_message(status, normalized),
            )

        evidences = (*facts, *derived)
        if self._has_conflicting_evidence(facts):
            return Answer(
                status=AnswerStatus.CONFLICTING_EVIDENCE,
                text="知识库中存在方向相反的证据，当前无法给出确定回答。",
                evidences=evidences,
                warnings=("请由专业人员核对冲突来源。",),
            )

        try:
            generated, active, degraded_from = self._generate(
                normalized, facts, generator
            )
        except GenerationError as error:
            return _generation_failure(evidences, error.error_code, requested_id, generator)
        except Exception:
            return _generation_failure(evidences, "internal_error", requested_id, generator)

        associations = _build_derived_associations(derived)
        warnings = ["回答仅基于当前知识库中召回的证据。"]
        if degraded_from is not None:
            warnings.insert(
                0,
                f"模型生成失败（{degraded_from}），已按 TRACEGRAPH_LLM_FALLBACK"
                "降级为离线摘录，本次回答的内容由摘录生成器产出。",
            )
        return Answer(
            status=AnswerStatus.ANSWERED,
            # 正文只由已通过校验的主张拼装，生成器的自由文本不进这里。
            text=compose_text(generated.claims),
            claims=generated.claims,
            derived_associations=associations,
            evidences=evidences,
            warnings=tuple(warnings),
            metrics={
                "evidence_count": float(len(evidences)),
                "derived_association_count": float(len(associations)),
                "max_hops": float(max_hops),
                "requested_generator": requested_id,
                "generator": self.registry.id_of(active),
                "model": _generator_model(active),
                "generation_degraded": degraded_from is not None,
            },
        )

    def _generate(
        self, question: str, facts: tuple[Evidence, ...], generator: AnswerGenerator
    ) -> tuple[GeneratedAnswer, AnswerGenerator, str | None]:
        """生成回答；仅在显式配置了降级生成器时才降级。

        第三个返回值是本次降级的起因（上游的 error_code），未降级时为 None。
        第二个返回值是真正产出答案的生成器 —— 降级时它与请求的模型不同。
        """
        try:
            return generator.generate(question, facts), generator, None
        except GenerationError as error:
            if self.fallback_generator is None:
                raise
            return (
                self.fallback_generator.generate(question, facts),
                self.fallback_generator,
                error.error_code,
            )

    def _has_conflicting_evidence(self, facts: tuple[Evidence, ...]) -> bool:
        """同源直接冲突：同一疾病对同一食物/药物同时给出正反关系。

        直接查图，不走遍历 —— 遍历的 fanout 截断只应影响召回，
        不应让正确性判定依赖某条关系是否恰好排进前 N 名。
        不同疾病给出相反建议是正常的，因此跨疾病相反关系不参与判定。
        """
        if self.graph is None:
            return False
        subject_id = _single_hop_subject(facts)
        if subject_id is None:
            return False
        return bool(
            self.graph.find_opposing_relations(
                subject_id, _OPPOSING_RELATION_TYPES, self.workspace_id
            )
        )


def _generation_failure(
    evidences: tuple[Evidence, ...],
    error_code: str,
    generator_id: str,
    generator: AnswerGenerator,
) -> Answer:
    return Answer(
        status=AnswerStatus.SYSTEM_ERROR,
        text=_GENERATION_FAILURE_TEXT,
        evidences=evidences,
        warnings=("检索已经完成，但生成阶段失败。",),
        # 失败的响应也带上生成器字段，前端不必为失败态单独分支。
        metrics={
            "generator": generator_id,
            "model": _generator_model(generator),
            "generation_degraded": False,
        },
        error_code=error_code,
    )


def _generator_model(generator: AnswerGenerator) -> str:
    """生成器背后的模型名；离线摘录没有模型名。"""
    return getattr(generator, "model", "")


def _split_by_hops(
    evidences: tuple[Evidence, ...],
) -> tuple[tuple[Evidence, ...], tuple[Evidence, ...]]:
    facts = []
    derived = []
    for evidence in evidences:
        path = evidence.graph_path
        if path is not None and path.hop_count > 1:
            derived.append(evidence)
        else:
            facts.append(evidence)
    return tuple(facts), tuple(derived)


def _build_derived_associations(evidences: tuple[Evidence, ...]) -> tuple[Claim, ...]:
    """把多跳证据渲染成独立区块；由本服务确定性拼装，LLM 不参与。"""
    associations = []
    for evidence in evidences:
        path = evidence.graph_path
        if path is None:
            continue
        summary = " ".join(evidence.content.split())
        if len(summary) > 180:
            summary = f"{summary[:177]}..."
        associations.append(
            Claim(
                text=f"{describe_path(path)}：{summary}。",
                evidence_ids=(evidence.id,),
            )
        )
    return tuple(associations)


def compose_text(claims: tuple[Claim, ...]) -> str:
    """由已通过校验的直接事实主张拼装回答正文。

    这是 `answer.text` 的唯一来源：生成器（离线摘录或大模型）都只提交主张，
    正文一律在这里确定性组装，因此正文里不可能出现任何未被主张承载的结论。
    """
    lines = [_ANSWER_HEADER]
    lines.extend(
        f"- {claim.text} [{index}]" for index, claim in enumerate(claims, start=1)
    )
    lines.append(_ANSWER_FOOTER)
    return "\n".join(lines)


def _single_hop_subject(facts: tuple[Evidence, ...]) -> str | None:
    """取一跳事实的起点实体；一跳路径都从同一个 top-1 实体出发。"""
    for evidence in facts:
        path = evidence.graph_path
        if path is not None and path.hop_count == 1:
            return path.start.id
    return None
