---
name: image-to-editable-ppt
description: Use when converting one or more uploaded images, screenshots, exported slides, or photographed presentation pages into high-fidelity editable 16:9 PPTX files.
---

# Image to Editable PPT

## 核心原则

把输入图片高保真复刻为可编辑 16:9 PPTX。事实正确优先于视觉高保真，视觉高保真优先于主要内容可编辑；禁止美化、自动平均、补造内容和整页图片化。普通文字与当前合同能够准确表达的图形、表格、连接关系和简单图表保持内容级原生可编辑；已有图片素材、独立图标与无法准确原生表达的视觉内容只保留为当页最小局部 picture。

每个非背景元素在全页盘点后必须读取[元素表达分类](references/element-representation.md)，按“主要文字分离 → 已有图片素材 → 独立图标 → 准确原生表达 → 最小局部 picture”的固定顺序逐个分类，首个命中项即为终态。不得以时间、对象数量、拆分可行性或整体相似为由跳步或改类。

schema v2 是唯一 Layout IR，`build_pptx_from_spec.py` 是唯一构建入口。精简流程不得削弱 Text Run、Paragraph、原生 bullet、表格 merge、connector、crop、background 与 OOXML 安全规则。

## 验证模式

`verification_profile` 和 `delivery_status` 必须显式写入每页规格，`verification_profile` 在一个批次内固定，不得依赖验证器补默认值。

- `rapid`：默认模式。读取并完整执行[rapid 交付](references/rapid-delivery.md)；无需 batch runtime preflight。
- `reviewed`：仅当用户明确要求额外视觉审查时使用。从任务开始到结束保持 `verification_profile=reviewed`，读取并完整执行[reviewed 视觉审查](references/reviewed-visual-audit.md)；不得继承 rapid 专属终态。

## 页面规格

新页面先使用复制式模板，从 Skill 根目录复制自包含 authoring 脚本；`SOURCE_PATH`、`PAGE_DIR` 和 `PAGE_ID` 必须替换成当前页的实际路径和 ID：

```bash
python3 scripts/init_page_authoring.py --source SOURCE_PATH --page-dir PAGE_DIR --page-id PAGE_ID --profile rapid
```

初始化必须早于页面事实编写。禁止以历史任务中的 `prepare_spec.py`、`finalize_spec.py` 或旧 Schema 示例作为脚手架；复制后的页面脚本不得导入共享 authoring helper。每页维护复制得到的 `prepare_spec.py`、`finalize_spec.py`、`work/page-reconstruction.json` 与当前 `work/page.pptx`。

展示 source 与 coordinate overlay 后，一次盘点全部元素和关系，盘点后按[元素表达分类](references/element-representation.md)逐元素固定 representation，再把明确的点与框合并为一次带语义 ID 的批量测量。只编辑 `prepare_spec.py` 的 `PAGE FACTS` 区；稳定前置区和稳定装配区负责确定性机械展开。页面事实区可使用局部函数、数组、推导式和循环，生成的 schema v2 仍是唯一 Layout IR。不得创建第二套 IR；不得直接修改生成的 JSON。

文字构造、overflow、wrap、margin、字体、裁切处理与普通视图门禁只按[文字与可编辑性](references/text-and-editability.md)执行。视觉或结构修复必须回到当前 profile 允许的修复阶段，只修改 `prepare_spec.py` 的 `PAGE FACTS` 并重新生成。

按页面内容读取条件 reference：

| 页面条件 | 必读 reference |
|---|---|
| 每个非空页面 | [元素表达分类](references/element-representation.md) |
| 每个非空页面 | [测量与布局](references/measurement-and-layout.md) |
| 普通/特殊文字、列表、表格文字 | [文字与可编辑性](references/text-and-editability.md) |
| 表格、矩阵、状态条、图示、连接线或图表 | [图形与图示](references/graphics-and-diagrams.md) |
| 图标、照片、Logo、截图、蒙版、背景或图片效果 | [图片与图标](references/pictures-and-icons.md) |

## 多页与交付

逐页执行；单页失败时保留诊断并继续后页，不得让一页覆盖另一页的状态或证据。合并资格、命令、验证和交付披露只按当前 profile reference 执行。
