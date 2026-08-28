# Reviewed 视觉审查

本文件只适用于 `verification_profile=reviewed`。它复用主 SKILL 的共享构建链；四项草稿门禁在下文直接定义，不读取或继承 rapid 状态机。

## 进入条件与状态

首次及每次修复后进入 prebuild 前，规格必须显式写 `delivery_status=pending`；prebuild 只接受 pending。先完成 structure、background、content completeness、main editability 四项共享草稿门禁：

- 任一门禁失败：写 `reviewed_failed`，保留诊断；该页既不能成功交付，也不能草稿合并。
- 四项全通过：页面具备 reviewed 失败时的草稿 fallback 资格，再进入 preview、共享基础判断和 reviewed 专属尾链。

reviewed 的 preview 是必需证据。当前哈希至多尝试一次；command error、`SIGABRT`、无 PDF 或 Poppler 缺失时不重试、不 preflight、不换字体、不重建，写 `reviewed_failed`。字体 fallback 本身不是 P0/P1，只有其造成的可见裁切、换行、溢出或层级差异才评级。

## 共享基础判断与基础集中修复

当前 preview 可用时，主代理先做一次整页基础语义判断，一次列全 mapping、区域比例、层级、主要文字裁切/换行、对象缺失或错绑定的 P0/P1。无 P0/P1 时不修复；存在不可修复 P0/P1 时直接写 `reviewed_failed`。

全部 P0/P1 可修复时，最多一次基础集中修复：只改 `prepare_spec.py`，重置 `delivery_status=pending`，从 prebuild 重建当前规格、PPTX 和四门禁证据，并为新哈希至多生成一次 preview。不在此处另做一次修复后视觉复核；随后的首次七类 coverage 审查同时验证基础问题已关闭。这个基础机会不消耗 reviewed 专属的额外修复机会。

## 整页视觉审查

主代理以只读 evaluator 身份读取当前 source、build spec snapshot、PPTX、build report、preview、structure report 和 background report，对 source 与 preview 执行一次整页视觉审查，一次列全 P0/P1，并产生七类 coverage。如果用过基础修复，该审查同时确认旧问题关闭且没有新增 P0/P1；如果未修复，它直接完成 reviewed 的首次判定：

1. `canvas_and_regions`
2. `objects_and_geometry`
3. `text_and_typography`
4. `tables_and_matrices`
5. `graphics_connectors_charts`
6. `pictures_crop_layers`
7. `high_risk_regions`

存在的类别写 `checked`，确实不存在的写 `not_applicable`。这七类是同一次整页判断的输出，不是七轮检查。结果必须写入当前规格的 `visual_gate.review`，不得放在无绑定的独立说明文件。

对 `can|cube|ellipse|arc|flowChart*` 等可能同时像业务 pictogram 和结构节点的 native Shape，本次审查必须回到 source 像素核对 `classification_basis/classification_evidence`：声称的 connector 或节点内标签必须真实可见，文字旁符号、重复图标槽位和统一 pictogram family 不得因规格中省略 `modules.icons` 而被当作节点。不符时按 P1 一次列全。这是 reviewed 的来源视觉核对，不新建独立 inventory。

- P0：PPTX 不可用、核心内容缺失、主要内容不可编辑、数据编造；
- P1：数量、比例、结构、fill、字号/换行、行/段距、框内位置、Text Run、bullet、crop、connector、图表或关键装饰错误；
- P2：不影响事实、可编辑性和主要版式的轻微色差、线宽或 renderer 近似。

无开放 P0/P1 时写五字段记录：

```json
{
  "mode": "main_agent_read_only_visual_audit",
  "decision": "passed",
  "coverage": {
    "canvas_and_regions": "checked",
    "objects_and_geometry": "checked",
    "text_and_typography": "checked",
    "tables_and_matrices": "not_applicable",
    "graphics_connectors_charts": "checked",
    "pictures_crop_layers": "checked",
    "high_risk_regions": "checked"
  },
  "repair_applied": false,
  "post_repair_verification": "not_required"
}
```

## reviewed 专属额外集中修复与修复后验证

首次七类审查存在不可修复 P0/P1 时直接写 `reviewed_failed`，不消耗 reviewed 修复机会。只有该审查仍发现全部可修复的 P0/P1 时，才触发最多一次 reviewed 专属额外集中修复：只改 `prepare_spec.py`，重置 `delivery_status=pending`，从 prebuild 重建当前规格、PPTX 和四门禁证据，并为新哈希至多生成一次 preview。如果先前已用基础修复，这是第二次且最后一次修复；如果未用基础修复，这是唯一实际修复。

随后只执行一次修复后验证：同一次复查确认旧问题关闭、无新增 P0/P1，并重新生成七类 coverage。只要基础或 reviewed 专属任一阶段实际修复过，通过时均在 `visual_gate.review` 写 `repair_applied=true`、`post_repair_verification=passed`；只有两个阶段都未修复时才写 `repair_applied=false`、`post_repair_verification=not_required`。仍有 P0/P1 或 preview 不可用时写 `reviewed_failed`，不得新增第三次修复、再次渲染或返回基础阶段。

## 轻量七产物 Final

视觉审查通过且所有 Final 所需声明均已完成后，先写入 `reviewed_passed`，再运行 Final。这个顺序是硬合同：Final 输入本身必须已经是 reviewed 成功终态，不能在 Final 报告生成后再补状态。

```bash
python3 scripts/validate_reconstruction_spec.py \
  work/page-reconstruction.json \
  --stage final \
  --output work/final-validation.json
```

Final 只读核对 source、build spec snapshot、当前 PPTX、build report、当前 preview、structure report 和 background report 的路径、SHA-256 与交叉身份，并要求 `final_report.visual_review_outcome == visual_gate.review`。它不运行 producer、LibreOffice、visual diff 或 runtime/font 解析。

Final 通过后，从 `work/final-validation.json` 生成到成功 merge 完成期间不得再修改规格、PPTX、preview 或任何证据；完整 `spec_sha256` 变化会使 Final report 失效。

多页成功合并时，按相同页序提供一一对应的 input、spec 和 `--final-report`：

```bash
python3 scripts/merge_pptx.py \
  --input page-001/work/page.pptx \
  --input page-002/work/page.pptx \
  --spec page-001/work/page-reconstruction.json \
  --spec page-002/work/page-reconstruction.json \
  --final-report page-001/work/final-validation.json \
  --final-report page-002/work/final-validation.json \
  --output final/deck-reviewed.pptx
```

## 失败 fallback

preview 不可用、开放 P0/P1、修复后验证失败或 Final 失败时写 `reviewed_failed`。只有四项共享草稿门禁仍全部通过，才允许 fallback：在当前失败终态下重新生成 draft report，再使用 `merge_pptx.py --draft` 并传入每页 input、spec、`--draft-report`。旧 Final report 不得复用。

如果 Final 失败发生在曾写 `reviewed_passed` 之后，先把当前规格改为 `reviewed_failed`；这会按设计废止旧 Final 身份。随后运行 `--stage draft` 重新生成 draft report，草稿 merge 绑定新的完整规格哈希。不得新增第三次修复、额外渲染或派生 delivery status。
