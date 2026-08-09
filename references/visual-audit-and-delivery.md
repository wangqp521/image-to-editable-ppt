# 视觉审计与交付

## 模式职责

`verification_profile` 必须显式写入每页规格并在批次内固定；用户未指定时使用 `rapid`。两种模式共用不带 `--runtime` 的 prebuild、compiler、structure、background、Text Run、图片和 OOXML 合同。只有 structure/background 均为 valid 且各自绑定当前 PPTX 实际 SHA-256 后，当前 PPTX 才具备草稿交付和 `--draft` 合并资格；代理调用 `--draft` 前必须完成此确认。

- `rapid`：草稿优先；主代理最多一次集中修复，preview 只作一次可选诊断。
- `reviewed`：完整执行 rapid 基础流程，再运行全新只读 Reviewer Round 1；仅当全部 P0/P1 可修复时，允许一次 reviewer 驱动集中修复和全新 Round 2 终局。

从任务开始到结束，reviewed 页面始终写 `verification_profile=reviewed`，不得先产生 rapid 终态再复用。主代理 rapid 阶段和 reviewer 阶段各有至多一次、不可转移的修复额度；这是编排合同，不增加状态字段、计数器或新的状态记录。

## 草稿、preview 与主代理修复

structure/background 报告必须均为 valid 并绑定当前 PPTX 哈希。任一报告无效或哈希未绑定，以及 PPTX 无法打开、页数/比例错误、结构损坏、核心内容缺失、数据编造、主要内容不可编辑、TextBox/Run 覆盖错误或图片化范围违反表示计划，都是硬失败，不能进入草稿交付或合并。

预览只在有助于视觉检查时运行一次，输出目录为 `preview/PPTX_SHA256`，其中 `PPTX_SHA256` 必须替换为当前 PPTX 的实际 SHA-256；preview 文件的直接父目录必须正是该 hash，禁止使用字面目录或复用旧 hash：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

首次 command error、`SIGABRT`、无 PDF/preview 或 Poppler 缺失且没有当前 preview 时，preview 不可用。不得 preflight、重试、切换 locale、改 fontconfig、换字体、重建 PPTX、运行 visual diff 或启动 reviewer。rapid 交付当前草稿；reviewed 写 `reviewed_failed` 并交付同一已通过 structure/background 的草稿。若 preview 已成功生成，字体 fallback、`pdffonts` mismatch 或 `matched=false` 仅是诊断，允许进入 reviewer。

有 preview 时一次核对整页 mapping、层级、文字、换行、图形、图表、crop、图标与背景。P0 包括 PPTX 不可用、核心内容缺失、主要内容不可编辑和数据编造；P1 包括数量、比例、结构、fill、字号/换行、行/段距、框内位置、Text Run、bullet、crop、connector、图表或关键装饰错误；P2 是轻微色差、线宽或 renderer 近似。字体 fallback 本身不是 P0/P1，只有造成可见裁切、错误换行、溢出或层级问题时才按可见差异评级。

主代理将全部可修复 P0/P1 按共同根因合入一次 `prepare_spec.py` 修改；以新哈希从 prebuild 起重建 build、structure、background，并至多再生成一次 preview。不得逐项边看边改、沿用旧哈希报告或把未使用主代理额度转给 reviewer。

## 独立 reviewer 有限流程

进入 Round 1 前，必须存在七类当前产物：source、build spec snapshot、当前 PPTX、build report、preview、structure report、background report，且每项都有当前绝对路径与 SHA-256；其中 structure/background 必须为 valid 并绑定当前 PPTX，preview 的直接父目录必须是当前 PPTX SHA-256。这是唯一可审集合；不要求 runtime preflight、字体 face/identity、render report、visual diff、region evidence 或旧 final report。

reviewer 使用全新只读上下文，只读取这七类当前产物并只返回契约 JSON；不运行 producer、不修改文件、不读历史产物或前序对话。Round 1 `passed` 后直接进行轻量 final 身份核对。Round 1 `changes_required` 且全部 P0/P1 可修复时，只作一次 reviewer 驱动集中修复，重建当前哈希、生成一次当前 preview，再提交全新 Round 2。Round 1 的 `not_reviewable`、无效响应、越界证据或不可修复问题，均写 `reviewed_failed` 并草稿交付。

Round 2 是终局：只有 `passed` 可以写 `reviewed_passed`；任何其他结果都不再修复、不再启动 reviewer，写 `reviewed_failed` 并交付草稿。`reviewed_passed` 只表示当前 PPTX 通过独立视觉复核和轻量哈希身份核对，不表示 renderer、字体文件或环境可复现闭环。

## 多页与交付

单页 prebuild/build/structure/background 硬失败时保留诊断并继续后页。rapid 和 `reviewed_failed` 的 structure/background 均 valid 且绑定当前 PPTX 哈希的页面使用 draft merger：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

draft merger 自身只重新运行每个输入的 PPTX structure validation，并验证合并后的整份 deck；它不接收或重验 background 报告或其哈希绑定。调用它之前，代理已按本节确认每页的 structure/background 当前报告。`reviewed_passed` 页面使用 input/spec/final-report 成功合并；其 final 只合并七产物当前身份和 reviewer outcome，不要求 renderer 或字体共同身份。

最终提供可编辑 PPTX、structure/background 报告及实际存在的 preview、reviewer response 和问题报告；披露缺页、开放 P0/P1、字体 fallback 与未验证项。只要 structure/background 均 valid 并绑定当前 PPTX，任何 preview 或 reviewer 失败都不得隐藏、拒绝或扣留草稿。
