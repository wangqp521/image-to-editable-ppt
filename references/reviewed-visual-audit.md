# Reviewed 视觉审查

## 进入条件

本文件只适用于 `verification_profile=reviewed`，从规格到交付全程保持 `verification_profile=reviewed`。先完成共享基础阶段：构建，structure/background、内容完整性和主要内容可编辑性硬门禁，首次 preview，一次初始整页语义判断，以及最多一次基础集中修复和其新哈希 preview。reviewed 在此阶段不执行 rapid 专属终局语义复核，也不写 rapid 终态；硬门禁均通过后，才进入 reviewed 专属尾链。

当前 PPTX 哈希已有成功 preview 时直接复用；尚未尝试时最多生成一次。当前哈希已因 command error、`SIGABRT`、无 PDF/preview 或 Poppler 缺失而失败时不重试、不 preflight、不换字体、不重建，写 `reviewed_failed` 并交付合格草稿。字体 fallback 本身不构成 P0/P1，只有可见裁切、错误换行、溢出或层级差异才评级。

## 整页视觉审查

首次七类 coverage 整页审查同时承担基础修复后的终局语义复核，不额外增加一次视觉判断。若共享基础阶段实施了基础修复，该审查同时检查已知问题关闭与新增 P0/P1；若未实施，则直接完成 reviewed 的首次七类判定。

主代理以只读 evaluator 身份读取当前 source、build spec snapshot、PPTX、build report、preview、structure report 和 background report，对 source 与 preview 执行一次整页视觉审查，同时一次列全 P0/P1 并产生七类 coverage：

1. `canvas_and_regions`
2. `objects_and_geometry`
3. `text_and_typography`
4. `tables_and_matrices`
5. `graphics_connectors_charts`
6. `pictures_crop_layers`
7. `high_risk_regions`

当页实际存在或激活的类别写 `checked`，确实不存在的类别写 `not_applicable`。coverage 是同一次整页审查的结果，不是额外审查阶段。

- P0：PPTX 不可用、核心内容缺失、主要内容不可编辑、数据编造。
- P1：数量、比例、结构、fill、字号/换行、行/段距、框内位置、Text Run、bullet、crop、connector、图表或关键装饰错误。
- P2：不影响事实、可编辑性和主要版式的轻微色差、线宽或 renderer 近似。

没有开放 P0/P1 时写五字段成功记录：

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

## 集中修复与修复后验证

存在不可修复 P0/P1 时直接写 `reviewed_failed`，不消费修复机会。全部 P0/P1 可修复时，按共同根因集中修改 `prepare_spec.py` 一次；从 prebuild 起重建 build、structure、background，并为新 PPTX 哈希至多生成一次 preview。

随后只执行一次修复后验证：对 source 与新 preview 作整页视觉复查，在同一次复查中确认旧问题关闭、没有引入新 P0/P1，并重新产生七类 coverage。通过时写 `repair_applied=true`、`post_repair_verification=passed`；仍有 P0/P1 或 preview 不可用时写 `reviewed_failed`，不得再次修改或渲染。

## 轻量七产物 Final 与交付

视觉审查无开放 P0/P1 时运行轻量七产物 Final。Final 只读核对 source、build spec snapshot、当前 PPTX、build report、当前 preview、structure report 和 background report 的路径、SHA-256 与交叉身份，并要求 `final_report.visual_review_outcome == visual_gate.review`；它不运行 producer、LibreOffice、visual diff 或 runtime/font 身份解析。

Final 通过后写 `reviewed_passed` 并进入成功合并。preview 不可用、存在开放 P0/P1、修复后验证失败或 Final 身份不一致时写 `reviewed_failed`；只要 rapid 硬门禁仍通过，就沿用 `--draft` 草稿路径。不得新增第三次修复、额外渲染或派生 delivery status。
