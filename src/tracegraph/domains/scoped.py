"""把工作空间自定义的抽取类型叠到已解析的适配器上。

这是「用户自定义领域」与「服务端不可变适配器目录」之间的唯一接缝，只有
extraction 与 review 两处调用它。适配器注册表本身依然没有写入入口：覆盖值
只活在这一个工作空间里，不动摇 domains/registry.py 的清单。

本模块只影响读类型清单的三条路径（提示词、候选白名单、人工审核校验）。问答
与生成链路不读这些，因此有意不经过这里 —— api.py 的 _require_adapter 保持
原样即可，不要当成漏网点去补。
"""

from dataclasses import dataclass

from tracegraph.core.contracts import AnswerStatus, ExtractionVocabulary, Workspace
from tracegraph.core.ports import DomainAdapter


def effective_adapter(workspace: Workspace, base: DomainAdapter) -> DomainAdapter:
    """返回这个工作空间真正生效的适配器。

    接的是**已经解析好的** base，本函数不做 resolve。三个调用点对「适配器不
    存在」的错误映射各不相同（extraction / review / api 各抛各的），在组合层
    再 resolve 一次会把这三种语义统一掉。
    """
    if (
        workspace.custom_entity_types is None
        and workspace.custom_relation_types is None
        and workspace.custom_vocabulary is None
    ):
        # 没有覆盖：原对象直传。这样绝大多数工作空间走的是改动前完全一样的
        # 对象，不是一层等价包装。
        return base
    return _ScopedAdapter(workspace=workspace, base=base)


@dataclass(frozen=True, slots=True)
class _ScopedAdapter:
    """在 base 之上叠加工作空间覆盖值的适配器。

    五个成员显式委托，不用 __getattr__ 转发：本项目没有静态类型检查兜底，
    魔法转发要等到运行期才发现漏了哪个成员。
    """

    workspace: Workspace
    base: DomainAdapter

    @property
    def name(self) -> str:
        # 记录抽取运行时用的是基础适配器的名字，因此一个自定义类型的工作空间
        # 产出的 ExtractionRun.adapter_id 仍然写 "medical" 之类。这是有损记录：
        # 该运行的候选类型可能和 registry 里 medical 声明的完全不同。
        return self.base.name

    @property
    def version(self) -> str:
        return self.base.version

    def entity_types(self) -> tuple[str, ...]:
        custom = self.workspace.custom_entity_types
        return self.base.entity_types() if custom is None else custom

    def relation_types(self) -> tuple[str, ...]:
        custom = self.workspace.custom_relation_types
        return self.base.relation_types() if custom is None else custom

    def extraction_vocabulary(self) -> ExtractionVocabulary | None:
        custom = self.workspace.custom_vocabulary
        return self.base.extraction_vocabulary() if custom is None else custom

    def normalize_question(self, question: str) -> str:
        return self.base.normalize_question(question)

    def preflight_status(self, question: str) -> AnswerStatus | None:
        return self.base.preflight_status(question)

    def status_message(self, status: AnswerStatus, question: str) -> str:
        return self.base.status_message(status, question)
