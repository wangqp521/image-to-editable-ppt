# 测量与布局

本文件只负责事实源、坐标、唯一 Layout IR 与通用布局关系；文字、图标、图形及 profile 尾链分别由对应 reference 负责。

## 事实源与坐标

污染无法确认时用 `direct_to_reconstruction`；只有拍摄透视/弯曲、反光/摩尔纹、环境背景、浏览器或聊天外壳、浮层/通知/遮挡、拼接或非内容边界可见时，才使用 `clean_with_imagegen`。清洗只做一次，提示词固定“请根据附件生成图片，要求高度还原，16:9，复刻源图片风格。”，不得美化、改字或改风格。

`content_reference` 唯一裁决文字、数字、单位、数量、分组和语义；`clean_visual_reference` 唯一裁决坐标、比例、颜色、字体观感、图标、纹理和层级。直通页两者均指原图；清洗页内容仍服从原图，清洗改动禁入 PPTX；页间不得借事实。

每页并行生成 coordinate overlay 与 source hash/尺寸，输出必须隔离。写规格前以“[第 N/总页数] 坐标定位图”展示 overlay；把 path/hash、source hash、grid、manifest 和 `inspection=passed` 写入 `coordinate_overlay_evidence`。来源或 grid 改变即重建。

展示后按 frame/mapping → regions → 锚点/层级 → 文字/数据 → 图标族/图片 → 颜色完成一次全页盘点。明确点位合成一次批量 `--point/--bbox` 测量；只有触边、邻近污染、遮挡/低清或报告无效时二测局部。

## 唯一 schema v2 规格

页面专用 `prepare_spec.py` 原子写出 `work/page-reconstruction.json`；schema v2 JSON 是唯一 Layout IR。页面脚本可以有局部函数、循环和数组，但不得创建共享 authoring helper、第二套对象清单或平行坐标合同。首次及修复后重新进入 prebuild 前必须显式写 `delivery_status=pending`；prebuild 拒绝任何终态。

页级必填骨架为：

`schema_version/page_id/verification_profile/delivery_status/session_reuse/content_reference/clean_visual_reference/canvas/activated_modules/modules/regions/elements/reading_order/visual_gate/editability_gate`。

`modules.representation_plan.items[]` 对每个来源事实只写以下公开字段：

`source_fact_id/visual_role/classification_basis/classification_evidence/source_bbox/required/render_mode/required_editability/fallback_policy/bound_element_ids/reason/coverage_status/evidence`。

`visual_role` 与 `render_mode` 是两个独立维度：

- 角色：`text|data|icon|pictogram|logo|photo|illustration|texture|ornament|container|connector|diagram_node|diagram_geometry|chart|background`。
- 模式：`native_text|native_shape|native_line|native_table|native_chart|composite_native|picture_asset`。
- fallback：`forbid|allow_minimal_asset|required_source_asset`。

`classification_basis` 只允许 `editable_text|editable_data|text_adjacent_symbol|repeated_icon_slot|standalone_semantic_symbol|literal_image|structural_container|connector_path|connector_endpoint_node|diagram_geometry|data_chart|not_applicable`。`classification_evidence` 是轻量关系记录，按 basis 只写必要字段；非歧义的文字、数据、普通连接线可写空对象。该对象严格只允许 `adjacent_text_fact_ids/repeat_group_id/attached_connector_fact_ids/contained_label_fact_ids/structural_boundary/full_contour_match`；事实 ID 必须回指当前 representation plan，`repeat_group_id` 必须回指当前 icon family。这些字段用于交叉校验，不是新 inventory。

先根据上下文确定 `classification_basis`，再确定 `visual_role`，最后按忠实表达能力和可编辑性选择 `render_mode`。preset 只能证明某种 native 模式可用，不能改变对象角色。`required=true` 的事实必须 `coverage_status=covered`、非空绑定、明确模式及非空证据；`required=false` 且 `not_applicable` 时模式才可为 null。

每个 element 记录唯一 `element_id`、`kind`、source/slide bbox、层级、可编辑性、置信度、style 与 content。native 事实的 `source_bbox` 必须与每个主绑定 element 的 `source_bbox` 完全一致；不得放大事实 bbox 吞入旁侧文字或连线，再用较小 Shape 充当节点。representation plan 是测量结论而非第二 IR；先完成计划再写 element，构建后不得补写。

## 画布、区域与关系

可信 16:9 页面按内容边界映射；其他比例使用等比 contain 和明确 offset，禁止拉伸。`canvas` 记录原图/视觉图尺寸、`page_frame_bbox`、slide EMU、mapping、offset、背景和内容范围。

`regions` 只记录实际存在区域及 source/slide bbox、padding、层级、阅读顺序和 element_ids。不得套模板、补区域、合并视觉上分离区域或自动平均栏宽、行高、间距。

`anchors/relationships/layout_invariants/density_targets` 保存来源中的边界、基线、中心线、包含/附着/重叠、阅读顺序、层级、留白、区域比例和视觉密度。原图轻微不齐或非均匀间距必须保留。

视觉上有两行以上文字或垂直对齐不是 `top` 的 TextBox，应在同一次批量测量中记录相邻可见行中心距和文字块相对框中心的纵向偏移，换算为 point 后写入 typography 的 `source_layout`。可见行不等于 Paragraph。

## 修正

生成顺序：页边界与映射 → 区域 → 锚点/关系 → elements → 局部文字、图形和图片。全局比例错先修 mapping/region；局部只修目标及相邻受影响对象。所有修正写回同一 `prepare_spec.py`，将 `delivery_status` 重置为 `pending`，再从 prebuild 重跑；禁用缩小字号、硬换行、移动单项或整页图片掩盖区域错误。
