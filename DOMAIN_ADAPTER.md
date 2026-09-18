# DomainAdapter 开发指南

领域适配器把通用问答流程与医疗、法律或企业制度规则分开。一个适配器需要实现：

- `entity_types()`：允许的实体类型；
- `relation_types()`：允许的关系类型；
- `extraction_vocabulary()`：离线（不调用模型）抽取的章节词汇表，没有就返回 `None`；
- `normalize_question()`：问题规范化；
- `preflight_status()`：检索前的拒答、升级或范围判断；
- `status_message()`：非正常回答状态的用户提示。

以及 `name` 与 `version` 两个属性。

医疗参考实现位于 `tracegraph.domains.medical`，包含 9 类实体、11 类关系、急症关键词和 DUTMed 结构化数据映射。

## 与「工作空间自定义类型」的分工

适配器注册表**不接受运行时注册**：`/adapters` 只读，服务端之外没有写入入口，这是有意的边界。

如果只是某个知识库需要另一套实体/关系类型，不必新增适配器：建库时传 `custom_types`，覆盖值存在 Workspace 上，只在那个知识库里生效（见 `tracegraph.domains.scoped`）。三项（`entity_types` / `relation_types` / `vocabulary`）各自独立，留 `null` 的项沿用适配器声明。生效值同时约束抽取提示词、候选白名单与人工审核校验。

注意 `ExtractionRun.adapter_id` 记录的始终是**基础**适配器的名字，因此自定义类型的知识库产出的运行记录看起来仍是 `general` 之类 —— 这是有损记录，判断类型范围要看 Workspace 的 `custom_types`。

新增领域时，应先准备小型固定语料和评测案例，再定义 Schema。不要把领域常量写进通用检索器或生成器，也不要仅通过更换提示词宣称完成领域适配。

领域数据导入器应满足两个条件：

1. 每条关系至少绑定一个真实 Chunk ID；
2. 同一记录重复导入不会产生重复文档版本、实体或关系。
