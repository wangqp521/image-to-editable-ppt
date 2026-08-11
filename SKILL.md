---
name: image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 核心原则

把输入图片高保真复刻为可编辑 16:9 PPTX。事实正确优先于视觉高保真，视觉高保真优先于主要内容可编辑；禁止美化、自动平均、补造内容和整页图片化。普通文字、基础图形、表格、连接线与图表应原生可编辑；照片、Logo、图标、插画、纹理、艺术字和复杂装饰只保留为当页最小局部 picture。

默认采用草稿优先的 `rapid`：先生成并完成与当前 PPTX 哈希绑定的 structure、background 验证，LibreOffice 只做一次可选、非阻断预览。字体回退、LibreOffice `SIGABRT`、缺少 Poppler 或预览失败都不得阻止交付已通过两项验证的可编辑草稿。

schema v2 是唯一 Layout IR，`build_pptx_from_spec.py` 是唯一构建入口。精简流程不得削弱 Text Run、Paragraph、原生 bullet、表格 merge、connector、crop、background 与 OOXML 安全规则。

## 验证模式

`verification_profile` 和 `delivery_status` 必须显式写入每页规格。`verification_profile` 在一个批次内固定；每次首次或修复后重新进入 prebuild 前，页面专用 `prepare_spec.py` 必须写 `delivery_status=pending`。不得依赖验证器补默认值。prebuild 冻结后只由页面专用 `finalize_spec.py` 把状态改为当前 profile 的终态：rapid 使用 `rapid_validated|rapid_validation_failed`，reviewed 使用 `reviewed_passed|reviewed_failed`。

- `rapid`：默认模式。读取[rapid 交付](references/rapid-delivery.md)；无需 batch runtime preflight。完成构建、structure/background 与内容硬门禁后，当前哈希 preview 最多尝试一次；preview 可用时由主代理执行首次整页语义判断并一次列全 P0/P1，最多允许一次基础集中修复。修复后重建自动证据、为新哈希最多生成一次 preview，再执行一次修复后终局语义复核；该复核只作终局判定，不得触发第二次修复。
- `reviewed`：用户明确要求额外视觉审查时使用。从任务开始到结束都写 `verification_profile=reviewed`；两种模式共享构建、硬门禁、首次 preview、首次语义判断、最多一次基础集中修复及其新哈希 preview。随后读取[reviewed 视觉审查](references/reviewed-visual-audit.md)；首次七类 coverage 审查同时吸收基础修复后的终局复核，不额外增加判断轮次，并保留最多一次 reviewed 专属额外集中修复及一次修复后视觉验证。

`rapid` 中，structure、background、内容完整性或主要内容可编辑性失败才阻断草稿交付与 `--draft` 合并。预览不可用时写 `rapid_validation_failed` 并交付已通过前两项验证的草稿；若 preview 已成功生成，字体回退只作诊断。不得把草稿称为视觉审查通过版。

## preferred font

字体是构建配置，不是独立流程。每页第一项 `modules.typography.items[].selected_font` 是该页 `preferred_font`；同页的 `selected_font`、`internal_font_declaration`、非空且非 `follow_text` 的 `bullet_font`、`font_name` 与 `font.name` 必须保持一致。compiler 把该字体写入文本、表格和图表的 `a:latin/a:ea/a:cs/a:sym`。

`preferred_font` 不要求预先证明字体已安装，也不要求 LibreOffice 精确解析。`pdffonts` 的实际字体只写入诊断报告；不替换规格字体、不重建 PPTX、不触发第二次渲染。用户未指定字体时，按当前平台选一个稳定的中文无衬线字体并在整页保持一致。

## 页面规格

每页维护 `prepare_spec.py`、`work/page-reconstruction.json` 与当前 `work/page.pptx`。展示 source 与 coordinate overlay 后，一次盘点全部元素和关系，把明确的点与框合并为一次批量测量。页面专用 Python 可使用局部函数、数组、推导式和循环生成完整 schema v2；不得创建第二套 IR 或直接修改生成的 JSON。

文字按来源 TextBox 一次转录为 `paragraphs_text`，主体样式覆盖全文，`spans` 只声明真实存在的局部样式差异。视觉或结构修复必须修改 `prepare_spec.py` 并重新生成。

简单 2D `pie|doughnut|column|bar|line` 在分类、系列、数值、轴、图例与标签均可确认时使用原生 Chart。3D、组合/双轴、趋势线、渐变纹理、平滑曲线、复杂阴影或证据不足时保留当页最小局部 picture。

按页面内容读取条件 reference：

| 页面条件 | 必读 reference |
|---|---|
| 每个非空页面 | [测量与布局](references/measurement-and-layout.md) |
| 普通/特殊文字、列表、表格文字 | [文字与可编辑性](references/text-and-editability.md) |
| 表格、矩阵、状态条、图示、连接线或图表 | [图形与图示](references/graphics-and-diagrams.md) |
| 图标、照片、Logo、截图、蒙版、背景或图片效果 | [图片与图标](references/pictures-and-icons.md) |

## rapid 核心链

从 Skill 根目录执行。规格必须先写齐，再用一次 prebuild 同时验证并冻结 exact bytes；compiler 只读 snapshot。

```bash
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage prebuild --snapshot work/build-spec-snapshot.json --output work/prebuild-validation.json
python3 scripts/build_pptx_from_spec.py --spec work/build-spec-snapshot.json --prebuild-report work/prebuild-validation.json --output work/page.pptx --build-report work/build-report.json --replace-current
python3 scripts/validate_pptx.py work/page.pptx --expected-slides 1 --spec work/build-spec-snapshot.json --build-report work/build-report.json --output evidence/PPTX_SHA256/structure-validation.json
python3 scripts/validate_background_contract.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report evidence/PPTX_SHA256/structure-validation.json --output evidence/PPTX_SHA256/background-contract.json
```

structure 与 background 均为 valid 且各自绑定当前 PPTX 实际 SHA-256 后，PPTX 才满足草稿交付和 `--draft` 合并条件。代理调用 `--draft` 前必须确认这两份当前报告；draft merger 本身只重新运行每个输入的 PPTX structure validation，随后验证合并后的 deck，不接收或重验 background 报告。只有需要视觉预览时才运行一次；命令中的 `PPTX_SHA256` 必须替换为当前 PPTX 的实际 SHA-256，生成 preview 文件的直接父目录必须正是该 hash，禁止使用字面目录或复用旧 hash 目录：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

macOS 必须从第一次就把 LibreOffice 放在允许启动应用的执行环境中运行；脚本使用独立可写 profile 和进程锁。`rapid` 直接预览只尝试一次：command error、`SIGABRT`、无 PDF 或 Poppler 缺失时记录为 preview 不可用，不重试、不运行 visual diff，继续交付已通过 structure/background 的 PPTX。若 preview 已成功生成，`pdffonts` mismatch、`matched=false` 或字体 fallback 仅记录诊断，不能否定该 preview。

当前哈希 preview 可用时，主代理执行且只执行一次整页语义视觉判断，核对 mapping、区域比例、层级、文字、换行、图形、表格、图表、crop、图片、图标和背景，并一次列全全部 P0/P1。没有 P0/P1 时写 `rapid_validated`；存在不可修复 P0/P1 或 preview 不可用时写 `rapid_validation_failed`。

全部 P0/P1 可修复时，最多一次基础集中修复：按共同根因只集中修改 `prepare_spec.py` 一次；从 prebuild 起重跑 build、structure、background，并为新 PPTX 哈希最多尝试一次 preview。新 preview 可用时，主代理再执行一次修复后终局语义复核，只检查已知问题是否关闭及是否新增 P0/P1。该复核不得触发第二次修复、再次渲染或 reviewed Final；无开放 P0/P1 时写 `rapid_validated`，仍有 P0/P1 或新 preview 不可用时写 `rapid_validation_failed`。

## reviewed 共享基础阶段 + 专属尾链

只有 `verification_profile=reviewed` 才读取并执行[reviewed 视觉审查](references/reviewed-visual-audit.md)。两种模式共享构建、硬门禁、首次 preview、一次初始整页语义判断、最多一次基础集中修复及其新哈希 preview；reviewed 不执行 rapid 专属的终局复核与 rapid 终态。其首次七类 coverage 整页审查同时承担基础修复后的终局语义复核，不额外增加视觉判断轮次。审查发现的全部 P0/P1 可修复时，最多进行一次 reviewed 专属额外集中修复、按新哈希重建并执行一次修复后验证。rapid 不得读取或执行该 reference。

## 多页与交付

逐页执行。单页 prebuild/build/structure/background 失败时保留诊断并继续后页；代理仅在调用前确认 structure/background 均为 valid 且绑定当前 PPTX 哈希时，才可提交页面进入草稿合并，不要求 runtime、preview 或 final report：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

草稿 merger 自身只重新检查每个输入为结构有效的单页 16:9 PPTX，并在合并后验证整份 deck；它不接收或重验 background/hash 报告。交付可编辑 PPTX、结构报告以及实际存在的预览/诊断；明确披露缺页、开放 P0/P1、字体回退与未验证项。不得因缺少 LibreOffice 证据隐藏或扣留已通过 structure/background 验证的草稿。
