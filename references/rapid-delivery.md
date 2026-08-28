# Rapid 交付

本文件只定义 `verification_profile=rapid` 的状态、可选诊断和草稿合并。rapid 不读取 reviewed 尾链。

## 状态机

首次及每次修复后进入 prebuild 前，规格必须显式写 `delivery_status=pending`。prebuild 只接受 pending。

rapid 的硬交付资格只由四项草稿门禁决定：

1. `structure`：当前单页 PPTX 可打开、16:9、结构有效，并与 structure report 的路径和 SHA-256 一致；
2. `background`：background contract valid，且绑定当前 page、content spec 与 PPTX；
3. `content_completeness`：`editability_gate.review.text_and_data=passed`；
4. `main_editability`：`editability_gate.status=passed`，且 `text_and_data/native_text_structure/basic_structure/full_slide_picture_risk` 全部 passed。

任一硬门禁失败时写 `rapid_validation_failed`，保留诊断并停止该页，不能生成有效 draft report、交付或合并。四项全部通过后，页面具备草稿交付资格；只有当前哈希 preview 可用、已完成整页语义判断且没有开放 P0/P1 时，才写 `rapid_validated`。主动跳过、preview 不可用，或一次集中修复后仍有开放 P0/P1 时写 `rapid_validation_failed`；只要四项门禁仍通过，后两种视觉失败仍可生成哈希绑定 draft report 并交付当前最佳草稿。摘要必须区分“硬门禁失败”“preview 未验证”和“视觉问题仍开放”。

preview 不属于四项草稿门禁：其缺失不能否定 structure、background、content completeness 或 main editability，也不能扣留已具备草稿资格的页面。但 preview 是 `rapid_validated` 的必需成功证据；主动跳过、LibreOffice command error、`SIGABRT`、无 PDF 或 Poppler 缺失时，不 preflight、不重试、不换字体、不重建，preview 不可用时写 `rapid_validation_failed`。`pdffonts` mismatch 或字体 fallback 本身只作诊断；preview 已成功且没有造成可见 P0/P1 时，不因此失败。

## 可选视觉诊断与一次修复

需要视觉诊断时，对当前 PPTX 哈希最多尝试一次：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

`PPTX_SHA256` 必须是当前 PPTX SHA-256，preview 文件的直接父目录必须正是该哈希。preview 可用时，主代理执行一次整页语义判断，一次列全 mapping、区域比例、层级、文字裁切/换行、框内位置、图形、表格、图表、crop、图片、图标和背景问题。所有发现项写入 `modules.high_risk.items[]`；P0/P1 只有 `result=passed` 才算关闭。`--stage draft` 对 `rapid_validated` 重新核对当前 preview 文件身份、父目录哈希、evidence 引用和已记录 P0/P1，不接受仅手写成功状态。

- P0：PPTX 不可用、核心内容缺失、主要内容不可编辑、数据编造；
- P1：数量、比例、结构、fill、字号/换行、行/段距、Text Run、bullet、crop、connector、图表或关键装饰错误；
- P2：不影响事实、可编辑性与主要版式的轻微色差、线宽或 renderer 近似。

全部 P0/P1 可修复时，最多进行一次集中修复：只改 `prepare_spec.py`，状态重置为 `pending`，从 prebuild 重跑 build、structure、background 和四门禁。可为新哈希再生成一次 preview，只用于确认已知问题是否关闭和是否新增 P0/P1；不得触发第二次修复。

一次集中修复已经消耗后，修复轮次即耗尽。仍有文字裁切、换行或其他开放 P0/P1 时，不得新增字体测量、自动字号搜索、额外修复轮次或新的硬交付门禁；四门禁仍通过时必须写 `rapid_validation_failed`，生成 draft report 并交付当前最佳草稿。交付摘要披露 `page_id/element_id/位置与裁切方向/已尝试修复/当前影响`。P0 若实际证明内容完整性或主要可编辑性门禁不应通过，则属于硬门禁失败，不得生成有效 draft report。

## Draft report 与合并

写入 `rapid_validated`，或因一次修复后仍有开放 P0/P1 而写入 `rapid_validation_failed` 后，运行 `--stage draft` 重新读取并冻结当前规格和四项证据：

```bash
python3 scripts/validate_reconstruction_spec.py \
  work/page-reconstruction.json \
  --stage draft \
  --output work/draft-validation.json
```

draft report 必须 `valid=true`、`errors=[]`，并绑定当前完整 `spec_sha256`、delivery status、content spec、PPTX、structure report 与 background report。`rapid_validated` 还必须绑定当前哈希 preview 且没有已记录的开放 P0/P1；`rapid_validation_failed` 不要求 preview，只在四项草稿门禁仍全部 passed 时才能得到有效 draft report。硬门禁失败始终拒绝。规格、状态或任一文件变化后旧报告立即失效。

多页草稿合并必须按相同顺序为每页提供一组 input、spec 和 `--draft-report`：

```bash
python3 scripts/merge_pptx.py --draft \
  --input page-001/work/page.pptx \
  --input page-002/work/page.pptx \
  --spec page-001/work/page-reconstruction.json \
  --spec page-002/work/page-reconstruction.json \
  --draft-report page-001/work/draft-validation.json \
  --draft-report page-002/work/draft-validation.json \
  --output final/deck-draft.pptx
```

merger 会重新验证每页当前规格、四门禁报告与 PPTX 哈希，再验证合并后的 deck；不得只传 PPTX，也不得把 reviewed Final report 传入 draft。交付草稿 PPTX、draft/structure/background 报告及实际存在的 preview/诊断，披露开放视觉问题；不得称为视觉审查通过版。
