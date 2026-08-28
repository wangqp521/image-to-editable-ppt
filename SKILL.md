---
name: image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 目标与不可破坏的原则

把每张来源图片高保真还原为 16:9 可编辑 PPTX。事实正确优先于视觉相似，视觉相似优先于次要素材可编辑；禁止美化、自动平均、补造看不清的内容和整页图片化。普通文字、数据、容器、基础图形、线、表格及证据充分的简单图表应原生可编辑；照片、Logo、图标、pictogram、插画、纹理和复杂装饰只保留为当页最小局部 picture。

每次任务从来源重新构建，不复用历史规格、PPTX、裁切资产、预览或验证报告。`schema v2` 是唯一 Layout IR，`build_pptx_from_spec.py` 是唯一构建入口；页面脚本只能生成完整规格，不能直接修改已生成 PPTX。

任何流程精简、profile 优化或性能改造都不得削弱已经支持的高保真合同：单一复合 TextBox 与连续 Text Run、多个 Paragraph、原生 `buChar|buAutoNum|buBlip`、表格 merge、connector、picture crop、五类 native chart、背景合同及 OOXML 安全写入。若新流程与这些能力冲突，保留能力并修改流程；不得用拆框、图片化、静默 fallback 或放宽验证来换取速度。

先用 `classification_basis/classification_evidence` 记录可核对的角色依据，再判断 `visual_role`，最后选择 `render_mode`。PowerPoint preset 清单只是 `native_shape` 能力白名单，不是对象分类器。文字旁、图标槽位或统一 pictogram family 中的业务符号必须是 `visual_role=icon|pictogram|logo` 与 `render_mode=picture_asset`；即使轮廓近似 `can|cube|arc` 也不能原生重画，同族不得混用。`diagram_node/native_shape` 必须同时具有结构边界、连接端点或内含标签事实，以及完整轮廓匹配；单纯“像某个 preset”不足以证明它是节点。

复杂视觉与主要文字无法从来源像素无损分离时，必须按 `required_editability` 失败关闭，不得烘焙文字或重复叠字。详细边界见[图片与图标](references/pictures-and-icons.md)和[图形与图示](references/graphics-and-diagrams.md)。

## 工作流

1. 确认页序、输出名和 `verification_profile`。未指定时使用 `rapid`；同一批次 profile 固定。
2. 读取[测量与布局](references/measurement-and-layout.md)，展示 source 与 coordinate overlay，一次盘点全部事实、关系、图标族和高风险区域。
3. 按页面内容读取条件 reference，生成页面专用 `prepare_spec.py` 与 `work/page-reconstruction.json`。首次及每次修复后进入 prebuild 前，规格必须显式写 `delivery_status=pending`。
4. 对每个来源事实写 `modules.representation_plan.items[]`：`source_fact_id/visual_role/classification_basis/classification_evidence/source_bbox/required/render_mode/required_editability/fallback_policy/bound_element_ids/reason/coverage_status/evidence`。分类证据只引用同一 representation plan 中的事实和已有 icon family，不建立第二套 inventory；先定表示法，再写 element。
5. 生成所需的当页局部资产；所有图标统一使用 `extract_icon_asset.py`，禁止页面专用裁切脚本。
6. 运行共享构建链，得到绑定当前哈希的 structure、background、内容完整性与主要内容可编辑性证据。
7. 按 profile 只读取一个尾链：默认[rapid 交付](references/rapid-delivery.md)；用户明确要求额外视觉审查时读取[reviewed 视觉审查](references/reviewed-visual-audit.md)。

`rapid` 的 preview 不属于四项草稿门禁，但 `rapid_validated` 必须绑定当前 PPTX SHA-256 目录下的成功 preview，完成一次整页语义判断，并且没有开放 P0/P1。主动跳过或 preview 不可用时写 `rapid_validation_failed`；四项草稿门禁仍全部通过时继续生成哈希绑定 draft report 并交付可编辑草稿，不得冒充视觉判断通过。

## 页面规格

每页维护 `prepare_spec.py`、`work/page-reconstruction.json`、`work/build-spec-snapshot.json` 与当前 `work/page.pptx`。视觉或结构修复必须修改 `prepare_spec.py` 并从 prebuild 重建；不得手改 JSON、PPTX 或旧报告。

文字按来源 TextBox 一次转录为一个 `modules.typography.items[]`，使用 `text/runs/paragraphs/text_box`；多色或局部样式由无重叠、无缺口的 Text Run 表达。页面 `prepare_spec.py` 使用 `scripts/lib/textbox_authoring.py::compile_textbox()`：作者期 `paragraphs_text/spans` 只作为立即编译输入，返回值直接是一个 schema v2 element 和一个 typography item，不形成第二套 IR，也不写入 JSON。helper 的输入原子是完整来源 TextBox，必须同时保留全文 Text Run、一个或多个 Paragraph、逐段原生 list 和多行 `source_layout`；单行单段只是同一 helper 的特例。helper 以无默认值的 keyword-only 参数显式接收 `vertical_alignment` 与作者期 `text_safety=free_text|container_bound`；先锁定文字逻辑结构并在页面坐标中应用整个 TextBox 的几何安全处理，再由 helper 编译 schema 结构，`text_safety` 不写入 schema，也不改变对象数、Run、Paragraph 或 bullet。首次构建即按来源 mapping 处理字号、横向 `0.5em` 安全区、单行框纵向安全高度、margin、wrap、垂直锚点和框内垂直位置，不把可预防裁切推迟到 preview，也不全局启用 AutoFit、字体文件度量或自动字号搜索。详见[文字与可编辑性](references/text-and-editability.md)。

简单 2D `pie|doughnut|column|bar|line` 只有在分类、系列、数值、轴、图例和标签均可确认时使用 `native_chart`。native chart 没有 crop 合同，`slide_bbox` 就是完整图表框；需要裁切的复杂图表必须改用最小 `picture_asset`，通过 picture 的 `mode/crop` 写入 `a:srcRect`，同时保持独立标题、单位与标签可编辑。3D、组合/双轴、趋势线、渐变纹理、平滑曲线、复杂阴影或证据不足同样走该 fallback。

| 页面条件 | 必读 reference |
|---|---|
| 每个非空页面 | [测量与布局](references/measurement-and-layout.md) |
| 普通/特殊文字、列表、表格文字 | [文字与可编辑性](references/text-and-editability.md) |
| 表格、矩阵、状态条、图示、连接线或图表 | [图形与图示](references/graphics-and-diagrams.md) |
| 图标、照片、Logo、截图、蒙版、背景或图片效果 | [图片与图标](references/pictures-and-icons.md) |

## 共享构建链

从 Skill 根目录执行。规格必须先写齐，prebuild 同时验证并冻结 exact bytes，compiler 只读 snapshot。`PPTX_SHA256` 必须替换为当前文件的实际 SHA-256。

```bash
python3 scripts/validate_reconstruction_spec.py work/page-reconstruction.json --stage prebuild --snapshot work/build-spec-snapshot.json --output work/prebuild-validation.json
python3 scripts/build_pptx_from_spec.py --spec work/build-spec-snapshot.json --prebuild-report work/prebuild-validation.json --output work/page.pptx --build-report work/build-report.json --replace-current
python3 scripts/validate_pptx.py work/page.pptx --expected-slides 1 --spec work/build-spec-snapshot.json --build-report work/build-report.json --output evidence/PPTX_SHA256/structure-validation.json
python3 scripts/validate_background_contract.py work/build-spec-snapshot.json --pptx work/page.pptx --build-report work/build-report.json --structure-report evidence/PPTX_SHA256/structure-validation.json --output evidence/PPTX_SHA256/background-contract.json
```

随后把当前 PPTX、structure report、background report、内容与可编辑性 review 的绝对路径和哈希写入当前规格的 `visual_gate/editability_gate`。所有可交付终态和所有合并都必须由 `--stage draft` 或 `--stage final` 生成的报告绑定当前完整规格与当前 PPTX；硬门禁失败终态不生成有效交付报告，旧哈希证据不可复用。

## 字体与交付

每页第一项 `modules.typography.items[].selected_font` 是 `preferred_font`。同页字体声明保持一致，并写入 `a:latin/a:ea/a:cs/a:sym`。字体是否安装、`pdffonts` mismatch 或 LibreOffice fallback 只作诊断，不改变构建字体，也不触发重新构建。

逐页执行；失败页保留诊断并继续后页。多页合并必须按页序提供一一对应的 input、spec 及 draft/final report。交付 PPTX、验证摘要及实际存在的 preview/诊断，并披露缺页、开放问题、字体替代与未验证项。草稿不得称为 reviewed 视觉审查通过版。
