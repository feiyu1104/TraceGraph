import inspect

import pytest

from tracegraph.core.ports import GraphRepository
from tracegraph.storage.graph import InMemoryGraphRepository, SQLiteGraphRepository


_PORT_METHODS = tuple(
    sorted(
        name
        for name, value in vars(GraphRepository).items()
        if callable(value) and not name.startswith("_")
    )
)


def _backends() -> list[type]:
    # Neo4j 只在装了 tracegraph[graph] 时才能导入；这里只看类，不建连接。
    pytest.importorskip("neo4j")
    from tracegraph.storage.neo4j import Neo4jGraphRepository

    return [InMemoryGraphRepository, SQLiteGraphRepository, Neo4jGraphRepository]


@pytest.mark.parametrize("method", _PORT_METHODS)
def test_every_backend_matches_the_port_signature(method: str) -> None:
    expected = [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(getattr(GraphRepository, method)).parameters.values()
    ]

    for backend in _backends():
        implementation = getattr(backend, method, None)
        assert implementation is not None, f"{backend.__name__} 未实现 {method}"
        actual = [
            (parameter.name, parameter.kind, parameter.default)
            for parameter in inspect.signature(implementation).parameters.values()
        ]
        assert actual == expected, f"{backend.__name__}.{method} 的签名与端口不一致"
