# 测量与布局

## 事实源与坐标准备

污染无法确认用 `direct_to_reconstruction`；仅见拍摄透视/弯曲、反光/摩尔纹/环境背景、浏览器/聊天外壳、浮层/通知/遮挡、拼接或非内容边界时用 `clean_with_imagegen`。理由只写可见事实，不按扩展名/渠道。清洗仅一次，提示词固定“请根据附件生成图片，要求高度还原，16:9，复刻源图片风格。”，不得美化、改字或改风格。

`content_reference` 唯一裁决文字/数字/单位/数量/分组/语义；`clean_visual_reference` 唯一裁决坐标/比例/颜色/字体观感/图标/纹理/层级。直通页均指原图；清洗页内容仍服从原图，清洗改动禁入 PPTX；页间不借事实。

`rapid` 与 `reviewed` 都不运行 runtime/font preflight。每页并行启动 coordinate overlay 和 source hash/尺寸；输出隔离，任一失败不得消费部分结果。写规格前以 `[第 N/总页数] 坐标定位图` 展示 PNG；同源每页一次。将 overlay path/hash、source hash、grid、manifest、`inspection=passed` 写入 `coordinate_overlay_evidence`。来源/grid 改变即重建。

展示后按 frame/mapping → regions → 锚点/层级 → 高风险文字/数据 → 图片/图标 → 颜色完成一次盘点。把明确点位合为一次重复 `--point/--bbox` 调用；仅触边、邻近污染、遮挡/低清或报告无效时二测对应局部，禁多轮小测量。

视觉上包含两行及以上文字，或框内垂直对齐不是 `top` 的 TextBox，纳入同一次批量测量：按可见文字行从上到下记录相邻行中心距，并记录可见文字块中心相对 TextBox 中心的纵向偏移，正值向下。像素量测使用页面 mapping 换算为 point，写入 typography 的可选 `source_layout`；自动折行和真实 Paragraph 共用这组视觉目标，不把视觉行误写成段落边界。

## 唯一 schema v2 规格

生成前由页面专用 Python 维护构建事实并原子写出 `work/page-reconstruction.json`；schema v2 JSON 是唯一 Layout IR。页面脚本可定义当前页局部函数和循环，但不得导入或创建共享 authoring helper、第二套对象清单或平行坐标合同。JSON 必须包含 `schema_version/page_id/verification_profile/delivery_status/session_reuse/content_reference/clean_visual_reference/canvas/activated_modules/modules/regions/elements/reading_order/visual_gate/editability_gate`，并继续遵守本节坐标和 element_id 规则。首次 prebuild 以及修复后按新哈希重新进入 prebuild 前，`prepare_spec.py` 必须显式写 `delivery_status=pending`，验证器不补默认值。prebuild 冻结后，页面专用 `finalize_spec.py` 只能把它回填为当前 profile 的合法终态，不得修改上述 Layout 内容。

`modules.representation_plan.items[]` 是每个来源语义事实进 compiler 前的测量结论：存事实/bbox/必需性、`native|composite|asset`、所需可编辑性、fallback policy、绑定 element、理由、coverage、非空证据。先定表示法再写 element；唯一工作规格完整后，由一次正式 `prebuild --snapshot` 验证并冻结 build 输入，通过才构建当前 PPTX。计划非第二 IR，禁构建后补写。

每个实际对象记录数量、pixel/EMU bbox、结构关系、样式、层级、可编辑性和 `high|medium|low` confidence。先判断视觉事实和语义对象，再选绘制方式；不能根据代码方便反推原图。同一内容哈希从 prebuild 开始的正式页面链只运行一次；修复后必须以新哈希从 `prebuild --snapshot` 起重建结构与背景证据。`rapid` 的可选预览最多重新运行一次。

## 画布、区域与关系

可信 16:9 页面按内容边界映射；其他比例使用等比 contain 和明确 offset，禁止拉伸。`canvas` 记录原图/视觉图尺寸、`page_frame_bbox`、slide EMU、mapping、offset、背景和内容范围。

`regions` 只记录实际存在的标题、主要内容、表格/图表、说明、图例、页脚等区域及 source/slide bbox、padding、层级、阅读顺序和 element_ids。不得套模板、补区域、合并视觉上分离区域或自动平均栏宽/行高/间距。

`anchors/relationships/layout_invariants/density_targets` 保存原图的边界、基线、中心线、包含/附着/重叠、阅读顺序、层级、留白、区域比例和对象/文字/线条/色彩密度。原图轻微不齐或非均匀间距应保留，不得为了整齐改变视觉重心。分组背景块逐个记录 bbox、颜色和层级，不能只生成表头色。

## 生成与修正

顺序：页边界与映射 → 主要区域 → 锚点/层级/阅读顺序 → elements → 局部文字/图形/图片。首次 preview 后一次检查全页；全局缩放/区域比例错先修全局，再把同根因影响的对象批量写入唯一综合修正集合。局部仅修目标及相邻受影响对象。修正写回单一当前版本并全链重验；禁用缩小字号/硬换行/移动单项掩盖区域错，禁整页图片兜底。

图片保宽高比；`cover` 须有焦点/偏移证据，禁裁主体。圆形须正圆。原图无线/渐变/效果时禁补造。`editable_object_count` 仅作结构证据，不证明质量。
