# 视觉审计与交付

## 模式职责

`verification_profile` 必须显式写入每页规格并在批次内固定；用户未指定时使用 `rapid`。两种模式共用不带 `--runtime` 的 prebuild、compiler、structure、background、Text Run、图片和 OOXML 合同。只有 structure/background 均为 valid 且各自绑定当前 PPTX 实际 SHA-256 后，当前 PPTX 才具备草稿交付和 `--draft` 合并资格；代理调用 `--draft` 前必须完成此确认。

- `rapid`：草稿优先；主代理最多一次集中修复，preview 只作一次可选诊断。
- `reviewed`：完整执行 rapid 基础流程，再由主代理进行一次额外只读视觉审查；若审查发现的 P0/P1 全部可修复，最多增加一次集中修复和一次修复后验证。

从任务开始到结束，reviewed 页面始终写 `verification_profile=reviewed`，不得先产生 rapid 终态再复用。rapid 基础阶段和 reviewed 扩展阶段各有至多一次、不可转移的修复机会；不新增 workflow state、修复计数器或外部视觉审查 JSON IR。

## 草稿与 preview

structure/background 报告必须均为 valid 并绑定当前 PPTX 哈希。任一报告无效或哈希未绑定，以及 PPTX 无法打开、页数/比例错误、结构损坏、核心内容缺失、数据编造、主要内容不可编辑、TextBox/Run 覆盖错误或图片化范围违反表示计划，都是硬失败，不能进入草稿交付或合并。

预览只在有助于视觉检查时运行，输出目录为 `preview/PPTX_SHA256`，其中 `PPTX_SHA256` 必须替换为当前 PPTX 的实际 SHA-256；preview 文件的直接父目录必须正是该 hash：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

按当前 PPTX 哈希判断 preview 尝试：

- rapid 已为当前哈希生成 preview 时，reviewed 直接复用，不重新渲染；
- 当前哈希从未尝试 preview 时，reviewed 可尝试一次；
- 当前哈希已因 command error、`SIGABRT`、无 PDF/preview 或 Poppler 缺失失败时，reviewed 不重试、不 preflight、不切换 locale/fontconfig、不换字体、不重建；写 `reviewed_failed` 并交付已通过 structure/background 的草稿；
- preview 成功后，字体 fallback、`pdffonts` mismatch 或 `matched=false` 只是诊断，不使 preview 失效。只有 fallback 造成的可见裁切、错误换行、溢出或层级问题才按视觉差异评级。

## 主代理额外视觉审查

额外视觉审查和修复后验证由当前主代理完成。主代理在这两个阶段是只读 evaluator：只读取当前 source、build spec snapshot、当前 PPTX、build report、preview、structure report 和 background report，不修改 spec/PPTX、不运行 producer、不读取历史产物。它不是外部子代理，也不生成额外提示、上下文或响应文件。

七类 coverage 必须逐项给出 `checked` 或 `not_applicable`；当前页面实际激活的类别必须为 `checked`：

1. `canvas_and_regions`
2. `objects_and_geometry`
3. `text_and_typography`
4. `tables_and_matrices`
5. `graphics_connectors_charts`
6. `pictures_crop_layers`
7. `high_risk_regions`

一次审查整页 mapping、区域比例、层级、文字、换行、图形、表格、图表、crop、图片、图标与背景，并一次列全问题：

- P0：PPTX 不可用、核心内容缺失、主要内容不可编辑、数据编造；
- P1：数量、比例、结构、fill、字号/换行、行/段距、框内位置、Text Run、bullet、crop、connector、图表或关键装饰错误；
- P2：不影响事实、可编辑性和主要版式的轻微色差、线宽或 renderer 近似。

没有开放 P0/P1 时，可写成功审查记录。成功记录必须精确包含五个字段：

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

`repair_applied` 只表示 reviewed 扩展阶段是否使用额外修复机会，不统计 rapid 基础阶段的修复。未使用额外修复时必须为 `false/not_required`；使用后只有修复后验证通过才允许写 `true/passed`。

## 集中修复与修复后验证

额外审查发现 P0/P1 时，先一次列全并按共同根因合并。存在不可修复 P0/P1 时不消费额外修复机会，直接写 `reviewed_failed`。全部 P0/P1 可修复时，只允许一次额外集中修改 `prepare_spec.py`；随后以新哈希从 prebuild 起重建 build、structure、background，并为新 PPTX 哈希至多尝试一次 preview。不得逐项边看边改、沿用旧哈希报告或把未使用的 rapid 修复机会转入 reviewed。

修复后验证仍由主代理只读完成，必须重新核对整页和全部七类 coverage，而不是只看已修区域。验证通过时写 `repair_applied=true`、`post_repair_verification=passed` 和 `reviewed_passed`。仍有 P0/P1、当前 preview 不可用或证据身份不一致时，写 `reviewed_failed`，不得再修改 content spec、PPTX 或 build content，也不得产生第三次修复。

## Final、合并与交付

`reviewed_passed` 的 final 重新读取七类当前产物，核对 source/spec/PPTX/build/preview/structure/background 的路径、SHA-256 和交叉身份，并要求 `final_report.visual_review_outcome == visual_gate.review`。它不要求 runtime preflight、字体 face/identity、render report、visual diff 或 region evidence。

`reviewed_passed` 页面使用 input/spec/final-report 成功合并，结果标签为“视觉审查通过版”。rapid 和 `reviewed_failed` 页面在 structure/background 均 valid 且绑定当前 PPTX 哈希时只能使用 draft merger：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

draft merger 自身只重新运行每个输入的 PPTX structure validation，并验证合并后的整份 deck；它不接收或重验 background 报告或其哈希绑定。最终提供可编辑 PPTX、structure/background 报告及实际存在的 preview 和问题说明；披露缺页、开放 P0/P1、字体 fallback 与未验证项。只要 structure/background 均 valid 并绑定当前 PPTX，任何 preview 或 reviewed 视觉审查失败都不得隐藏、拒绝或扣留草稿。
