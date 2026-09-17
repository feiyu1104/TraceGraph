from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Iterable, Mapping

from tracegraph.core.contracts import Entity, Relation
from tracegraph.core.ports import DocumentRepository, GraphRepository
from tracegraph.ingestion.service import TextIngestionService


_RELATION_FIELDS = (
    ("category", "分类", "Category", "BELONGS_TO"),
    ("symptom", "症状", "Symptom", "HAS_SYMPTOM"),
    ("acompany", "并发症", "Disease", "ACCOMPANIES"),
    ("cure_department", "就诊科室", "Department", "TREATED_BY"),
    ("cure_way", "治疗方式", "Treatment", "USES_TREATMENT"),
    ("check", "检查", "Check", "REQUIRES_CHECK"),
    ("recommand_drug", "推荐药物", "Drug", "RECOMMENDS_DRUG"),
    ("common_drug", "常用药物", "Drug", "COMMONLY_USES_DRUG"),
    ("do_eat", "宜吃", "Food", "SHOULD_EAT"),
    ("not_eat", "忌吃", "Food", "SHOULD_NOT_EAT"),
    ("recommand_eat", "推荐食谱", "Recipe", "RECOMMENDS_RECIPE"),
)

_TEXT_FIELDS = (
    ("desc", "简介"),
    ("prevent", "预防"),
    ("cause", "病因"),
)


@dataclass(frozen=True, slots=True)
class MedicalImportResult:
    records: int
    skipped: int
    documents: int
    entities: int
    relations: int


class MedicalRecordImporter:
    def __init__(
        self,
        document_repository: DocumentRepository,
        graph_repository: GraphRepository,
    ) -> None:
        self.documents = document_repository
        self.graph = graph_repository
        self.ingestion = TextIngestionService(document_repository)

    def import_records(
        self, records: Iterable[Mapping[str, object]]
    ) -> MedicalImportResult:
        record_count = 0
        skipped_count = 0
        document_ids: set[str] = set()
        entity_ids: set[str] = set()
        relation_ids: set[str] = set()
        disease_relation_ids: dict[str, set[str]] = {}
        for record in records:
            name = str(record.get("name") or "").strip()
            if not name:
                skipped_count += 1
                continue
            content = _record_to_markdown(name, record)
            if content.strip() == f"# {name}":
                skipped_count += 1
                continue
            record_count += 1
            source_name = f"dutmed-{name}.md"
            result = self.ingestion.ingest_text(source_name, content)
            document_ids.add(result.document.id)
            chunks_by_locator: dict[str, list[str]] = {}
            for chunk in result.chunks:
                chunks_by_locator.setdefault(chunk.locator, []).append(chunk.id)

            disease = _entity("Disease", name)
            relation_ids.difference_update(disease_relation_ids.get(disease.id, set()))
            current_relation_ids: set[str] = set()
            targets: dict[str, Entity] = {}
            relations: list[Relation] = []
            entity_ids.add(disease.id)
            for field, heading, entity_type, relation_type in _RELATION_FIELDS:
                values = _as_strings(record.get(field))
                evidence = tuple(chunks_by_locator.get(f"{name} > {heading}", ()))
                if not evidence:
                    continue
                for value in values:
                    target = _entity(entity_type, value)
                    relation = Relation(
                        id=_stable_id("rel", disease.id, relation_type, target.id),
                        source_entity_id=disease.id,
                        target_entity_id=target.id,
                        type=relation_type,
                        evidence_chunk_ids=evidence,
                    )
                    targets[target.id] = target
                    relations.append(relation)
                    entity_ids.add(target.id)
                    relation_ids.add(relation.id)
                    current_relation_ids.add(relation.id)
            self.graph.replace_outgoing_graph(
                disease, tuple(targets.values()), tuple(relations)
            )
            disease_relation_ids[disease.id] = current_relation_ids

        return MedicalImportResult(
            records=record_count,
            skipped=skipped_count,
            documents=len(document_ids),
            entities=len(entity_ids),
            relations=len(relation_ids),
        )

    def import_jsonl(self, path: Path, limit: int | None = None) -> MedicalImportResult:
        records = []
        with path.open("r", encoding="utf-8") as stream:
            for line in stream:
                if line.strip():
                    records.append(json.loads(line))
                    if limit is not None and len(records) >= limit:
                        break
        return self.import_records(records)


def _record_to_markdown(name: str, record: Mapping[str, object]) -> str:
    sections = [f"# {name}"]
    for field, heading in _TEXT_FIELDS:
        value = str(record.get(field) or "").strip()
        if value:
            sections.extend((f"## {heading}", value))
    for field, heading, _, _ in _RELATION_FIELDS:
        values = _as_strings(record.get(field))
        if values:
            sections.extend((f"## {heading}", "、".join(values)))
    return "\n\n".join(sections)


def _as_strings(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(item).strip() for item in value if str(item).strip())
    if value is None:
        return ()
    normalized = str(value).strip()
    return (normalized,) if normalized else ()


def _entity(entity_type: str, name: str) -> Entity:
    return Entity(
        id=_stable_id("ent", entity_type.casefold(), name.casefold()),
        name=name,
        type=entity_type,
    )


def _stable_id(prefix: str, *parts: str) -> str:
    raw = "\x1f".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:20]}"
