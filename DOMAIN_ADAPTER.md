# DomainAdapter 开发指南

领域适配器把通用问答流程与医疗、法律或企业制度规则分开。一个适配器需要实现：

- `entity_types()`：允许的实体类型；
- `relation_types()`：允许的关系类型；
- `normalize_question()`：问题规范化；
- `preflight_status()`：检索前的拒答、升级或范围判断；
- `status_message()`：非正常回答状态的用户提示。

医疗参考实现位于 `tracegraph.domains.medical`，包含 9 类实体、11 类关系、急症关键词和 DUTMed 结构化数据映射。

新增领域时，应先准备小型固定语料和评测案例，再定义 Schema。不要把领域常量写进通用检索器或生成器，也不要仅通过更换提示词宣称完成领域适配。

领域数据导入器应满足两个条件：

1. 每条关系至少绑定一个真实 Chunk ID；
2. 同一记录重复导入不会产生重复文档版本、实体或关系。
