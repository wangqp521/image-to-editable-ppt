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

`verification_profile` 必须显式写入每页规格，并在一个批次内固定。

- `rapid`：默认模式。无需 batch runtime preflight；构建、结构和背景是主链，预览是可选诊断。主代理最多集中修复一次。
- `reviewed`：用户明确要求独立复核时使用。从任务开始到结束都写 `verification_profile=reviewed`，但完整执行 rapid 基础链，再增加全新只读 reviewer Round 1，以及最多一次 reviewer 驱动集中修复和 Round 2 终局复核。

`rapid` 中，structure、background、内容完整性或主要内容可编辑性失败才阻断草稿交付与 `--draft` 合并。预览不可用时写 `rapid_validation_failed` 并交付已通过前两项验证的草稿；若 preview 已成功生成，字体回退只作诊断。不得把草稿称为独立复核通过版。

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
| 视觉审核、终态与交付 | [视觉审计与交付](references/visual-audit-and-delivery.md) |

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

若预览成功，主代理核对整页 mapping、层级、文字、换行、图形、图表、crop、图标与背景，一次列全 P0/P1；同根因问题合并为一次集中修复。修复后从 prebuild 起重建，并重新执行结构、背景及最多一次预览。字体回退不是修复理由。

## reviewed = rapid + 独立复核

该模式的 prebuild、build、structure 和 background 与 rapid 完全共用，均不传 `--runtime`；只有 structure/background 均为 valid 且绑定当前 PPTX 实际 SHA-256 后，当前 PPTX 才可草稿交付或 `--draft` 合并。preview 只尝试一次，目录为 `preview/<当前PPTX实际SHA-256>`，其中 preview 文件的直接父目录必须是该 hash。首次 command error、`SIGABRT`、无 PDF 或 Poppler 缺失而没有当前 preview 时，不 preflight、不重试、不换字、不重建、不启动 reviewer；写 `reviewed_failed` 并交付草稿。

有当前 preview 时，reviewer 只能读取当前 source、build spec snapshot、当前 PPTX、build report、preview、structure report 和 background report；不要求 runtime/font identity/render report/visual diff/regions。preview 已成功生成时，`pdffonts` mismatch、`matched=false` 或字体 fallback 不阻止 reviewer，且 fallback 本身不是 P0/P1，只有可见裁切、错误换行、溢出或层级差异才评级。Round 1 `passed` 直接做只读轻量身份核对；`changes_required` 且全部 P0/P1 可修复时，按共同根因仅作一次 reviewer 驱动集中修复，然后从 prebuild 重建并生成一次当前 preview，交给全新 Round 2。Round 2 只有 `passed` 可写 `reviewed_passed`；其他结果不修复、不启动 Round 3，写 `reviewed_failed` 并交付草稿。

主代理 rapid 阶段最多一次修复、reviewer 阶段最多一次修复；这是不转移的编排合同，不新增状态字段或修复计数器。`reviewed_passed` 仅表示当前 PPTX 已通过独立视觉复核和轻量哈希身份核对，不表示 renderer 或字体文件可复现闭环。详细证据与合并规则见[视觉审计与交付](references/visual-audit-and-delivery.md)。

## 多页与交付

逐页执行。单页 prebuild/build/structure/background 失败时保留诊断并继续后页；代理仅在调用前确认 structure/background 均为 valid 且绑定当前 PPTX 哈希时，才可提交页面进入草稿合并，不要求 runtime、preview 或 final report：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

草稿 merger 自身只重新检查每个输入为结构有效的单页 16:9 PPTX，并在合并后验证整份 deck；它不接收或重验 background/hash 报告。交付可编辑 PPTX、结构报告以及实际存在的预览/诊断；明确披露缺页、开放 P0/P1、字体回退与未验证项。不得因缺少 LibreOffice 证据隐藏或扣留已通过 structure/background 验证的草稿。
