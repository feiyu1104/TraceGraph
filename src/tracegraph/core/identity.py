"""图实体与图关系的稳定身份规则。

发布候选知识时，同一个 Workspace 里同类型、同规范名称的实体必须落到同一个
图实体上，重复发布同一条关系也不能多出第二条边。因此 ID 既不能是随机的，也
不能只按名称生成 —— 它必须把归属、类型和规范化名称一起包含进去。

规则放在 core 而不是发布服务里：三个图后端、发布服务与一致性检查读的是同一
份定义，换实现不会换 ID。
"""

import hashlib


# 名称首尾这些字符不参与身份判断："(咳嗽)" 与 "咳嗽" 是同一条候选。
_EDGE_PUNCTUATION = " \t\r\n　.。,，;；:：、!！?？\"'“”‘’()（）[]【】{}<>《》"


def normalize_name(name: str) -> str:
    """名称的基础规范化：折叠空白、剥掉首尾标点、统一大小写。

    只做这些。同义词归并需要领域知识，不属于这里 —— 猜错的归并会把两条
    不同的知识合并成一条，比留着两条重复更难发现。
    """
    collapsed = " ".join(name.split())
    stripped = collapsed.strip(_EDGE_PUNCTUATION)
    return (stripped or collapsed).casefold()


def graph_entity_id(workspace_id: str, entity_type: str, normalized_name: str) -> str:
    """图实体 ID：同一 Workspace 内同类型、同规范名称的实体共用一个 ID。

    类型参与哈希，因此「百日咳」这个疾病与同名的其他类型实体不会被并成一个
    节点；Workspace 参与哈希，因此两个 Workspace 可以各有一个同名同类型的
    实体而互不影响。
    """
    if not workspace_id.strip():
        raise ValueError("图实体必须给出 workspace_id")
    if not entity_type.strip() or not normalized_name.strip():
        raise ValueError("图实体必须给出类型与规范化名称")
    return _stable_id("ent", workspace_id, entity_type.casefold(), normalized_name)


def graph_relation_id(
    workspace_id: str,
    source_entity_id: str,
    relation_type: str,
    target_entity_id: str,
) -> str:
    """图关系 ID：归属 + 两端 + 类型确定一条边，重复发布因此不会多出一条。"""
    if not workspace_id.strip():
        raise ValueError("图关系必须给出 workspace_id")
    for value in (source_entity_id, relation_type, target_entity_id):
        if not value.strip():
            raise ValueError("图关系必须给出两端实体与关系类型")
    return _stable_id(
        "rel", workspace_id, source_entity_id, relation_type, target_entity_id
    )


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:20]}"
