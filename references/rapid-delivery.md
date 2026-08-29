# Rapid 交付

## 适用范围

本文件定义 rapid 专属的停止与交付合同。只有 `verification_profile=rapid` 时才读取本文件；`reviewed` 不读取、不继承 rapid 专属终态尾链，而是按其独立 reference 执行共享基础阶段和 reviewed 专属尾链。

每次首次或修复后重新进入 prebuild 前，页面规格必须显式写 `delivery_status=pending`；验证器不补默认值。完成视觉判断后只写 rapid 终态：`rapid_validated` 或 `rapid_validation_failed`。

## 草稿资格

prebuild、build、structure 和 background 均不传 `--runtime`。structure/background、内容完整性和主要内容可编辑性硬门禁必须全部通过并绑定当前 PPTX 实际 SHA-256；TextBox/Run、图片化范围和 PPTX 可打开性也必须满足硬门禁。

任一硬门禁失败时保留诊断并停止当前页面，不能进入视觉判断、草稿交付或合并。硬门禁全部通过后，页面才具备可编辑草稿交付资格。

## Preview 与一次视觉判断

preview 可按需对当前 PPTX 哈希尝试一次：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

`PPTX_SHA256` 必须替换为当前 PPTX 的实际哈希，preview 文件的直接父目录必须正是该哈希。同一 PPTX 哈希最多尝试一次。command error、`SIGABRT`、无 PDF/preview 或 Poppler 缺失时不 preflight、不重试、不切换 locale/fontconfig、不换字体、不重建，写 `rapid_validation_failed`；只要硬门禁仍通过，继续交付可编辑草稿。

preview 可用时，主代理执行一次整页语义视觉判断，核对 mapping、区域比例、层级、文字、换行、图形、表格、图表、crop、图片、图标和背景，并一次列全全部 P0/P1。文字必须在普通视图完整显示；仅在鼠标悬停或双击编辑态显示完整仍属于固定 TextBox 裁切，不能判为通过。没有 P0/P1 时写 `rapid_validated`；存在不可修复 P0/P1 时写 `rapid_validation_failed`；全部 P0/P1 可修复时进入唯一一次基础集中修复。字体 fallback、`pdffonts` mismatch 或 `matched=false` 本身只作诊断，只有造成可见裁切、错误换行、溢出或层级差异时才列为视觉问题。

- P0：PPTX 不可用、核心内容缺失、主要内容不可编辑、数据编造。
- P1：数量、比例、结构、fill、字号/换行、行/段距、框内位置、Text Run、bullet、crop、connector、图表或关键装饰错误。
- P2：不影响事实、可编辑性和主要版式的轻微色差、线宽或 renderer 近似。

## 基础集中修复

最多一次基础集中修复：按共同根因只集中修改 `prepare_spec.py` 一次；从 prebuild 起重跑 build、structure、background，并为新 PPTX 哈希最多尝试一次 preview。不得复用旧哈希报告，也不得逐问题反复构建。

新 preview 可用时，主代理执行一次修复后终局语义复核，检查旧问题是否关闭以及是否新增 P0/P1。该终局复核不得触发第二次修复、再次渲染或 reviewed Final。无开放 P0/P1 时写 `rapid_validated`；仍有 P0/P1 或新 preview 不可用时写 `rapid_validation_failed`。只要硬门禁仍通过，失败后仍继续交付 draft。

## 交付

`rapid_validated` 和 `rapid_validation_failed` 页面只要当前硬门禁仍通过，就使用 draft merger：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

draft merger 重新验证输入和合并后 deck 的 PPTX 结构；调用前必须已经确认每页 structure/background 当前哈希绑定。交付可编辑 PPTX、结构/背景报告以及实际存在的 preview/诊断，披露开放问题和不可用证据；不得因 preview 缺失或视觉校验失败扣留已经通过硬门禁的草稿，也不得把 rapid 草稿称为 reviewed 视觉审查通过版。
