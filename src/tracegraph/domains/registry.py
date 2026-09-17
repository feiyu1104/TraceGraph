"""受信任的内置领域适配器目录。

注册表只装服务端自己构造的适配器：没有从请求、文件或配置里注册新适配器
的入口，因此这里的 ID 清单就是全部可选值。构造完成后注册表没有任何写入
方法，也就不存在「请求把适配器改掉」的路径。
"""

from dataclasses import dataclass

from tracegraph.core.ports import DomainAdapter
from tracegraph.domains.general import GeneralDomainAdapter
from tracegraph.domains.medical.adapter import MedicalDomainAdapter
from tracegraph.domains.personal_notes import PersonalNotesDomainAdapter


class UnknownAdapterError(ValueError):
    """adapter_id 不在注册表里。"""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"未知的领域适配器：{adapter_id}")
        self.adapter_id = adapter_id


class DuplicateAdapterError(ValueError):
    """两个内置适配器用了同一个 ID。"""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"领域适配器 ID 重复：{adapter_id}")
        self.adapter_id = adapter_id


@dataclass(frozen=True, slots=True)
class AdapterEntry:
    """一条登记项：适配器本身，加上对外描述用的元信息。"""

    adapter: DomainAdapter
    label: str
    description: str
    builtin: bool = True

    @property
    def id(self) -> str:
        # 适配器的 name 就是它的 ID，因此清单里的 ID 不可能和实际用的对不上。
        return self.adapter.name

    def describe(self) -> dict[str, object]:
        """`GET /adapters` 的单个条目。

        只含领域类型清单与说明文字：提示词、代码路径、模型密钥和内部配置
        都不在这里，也不在适配器对象的任何可序列化字段里。
        """
        return {
            "id": self.id,
            "label": self.label,
            "description": self.description,
            "version": self.adapter.version,
            "entity_types": list(self.adapter.entity_types()),
            "relation_types": list(self.adapter.relation_types()),
            "builtin": self.builtin,
        }


class AdapterRegistry:
    """一次装配好的适配器目录。

    重复 ID 在构造时就被拒绝：真出现这种错，应该在上线前炸掉，而不是让
    后注册的那个悄悄盖掉前一个。
    """

    def __init__(self, entries: tuple[AdapterEntry, ...]) -> None:
        by_id: dict[str, AdapterEntry] = {}
        for entry in entries:
            if entry.id in by_id:
                raise DuplicateAdapterError(entry.id)
            by_id[entry.id] = entry
        self._entries = entries
        self._by_id = by_id

    def resolve(self, adapter_id: str) -> DomainAdapter:
        """按 ID 取适配器；未知 ID 抛 UnknownAdapterError。"""
        entry = self._by_id.get(adapter_id)
        if entry is None:
            raise UnknownAdapterError(adapter_id)
        return entry.adapter

    def describe(self) -> dict[str, object]:
        """`GET /adapters` 的响应体；顺序就是注册顺序，因此每次调用都一样。"""
        return {"adapters": [entry.describe() for entry in self._entries]}


def build_default_adapter_registry() -> AdapterRegistry:
    """三个内置适配器。

    medical 必须排在第一位并保持 ID 不变：ws-default 记录的就是它。
    """
    return AdapterRegistry(
        (
            AdapterEntry(
                adapter=MedicalDomainAdapter(),
                label="医疗",
                description="面向疾病、症状、检查、用药与饮食等医疗知识。",
            ),
            AdapterEntry(
                adapter=GeneralDomainAdapter(),
                label="通用",
                description="面向普通资料、说明文档与通用知识。",
            ),
            AdapterEntry(
                adapter=PersonalNotesDomainAdapter(),
                label="个人笔记",
                description="面向个人笔记、项目记录与日常资料。",
            ),
        )
    )
