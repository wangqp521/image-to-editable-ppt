# 文字与可编辑性

本文件只定义 schema v2 的文字合同、固定 TextBox 容量、字体和最低可编辑性；流程状态由 profile reference 负责。

## 文字合同

每个来源 TextBox 对应一个 `kind=text` element 和一个 `modules.typography.items[]`。Typography item 必须完整写：

`element_id/text/source_font_guess/selected_font/fallback_reason/fallback_trace/runs/paragraphs/text_box/internal_font_declaration/font_declaration_verified`。

来源存在两行以上可见文字或垂直对齐不是 `top` 时，再写 `source_layout`。element 的 `content.text`、typography 的 `text` 和 OOXML 可见文本必须完全一致；`text_box.x/y/w/h` 与 element `slide_bbox` 一致。

### 作者期复合 TextBox helper

来源 TextBox 是 `scripts/lib/textbox_authoring.py::compile_textbox()` 的输入原子。一次 helper 调用生成一个 `kind=text` element 和一个 `modules.typography.items[]`，直接组装 schema v2 的 `text/runs/paragraphs/list/source_layout/text_box`。单行单段文字是同一 helper 的特例，不是另一条简化文字路径。

页面脚本按固定顺序处理一个来源 TextBox：

1. 一次转录框内完整 `text`，不按可见行创建多个 element。
2. 用连续 `runs[]` 保留框内多色、字重、字号和局部样式。
3. 用一个或多个 `paragraphs[]` 保留真实段落；同源列表的每项在该 TextBox 内使用原生 list Paragraph。
4. 多行可见文字用 `source_layout` 记录行中心距和文字块中心偏移。
5. 锁定内容结构后再应用 `text_safety`；安全处理只改整个 TextBox 的 source bbox、margin 与容量，不改变 element 数、Run 区间、Paragraph 边界或 bullet 类型。

作者期使用 `paragraphs_text` 一次提供完整单段字符串或真实 Paragraph 字符串列表；这些字符串直接拼成 schema `text`，Paragraph 边界直接编译到 `paragraphs[]` 与 `text_box.paragraph_breaks`，不在文本中重复写 CR/LF。`spans` 只写相对主体样式的真实差异：优先使用唯一 `text`，重复文本必须增加从 1 开始的 `occurrence` 或显式 `[start,end)`；定位不唯一时 helper 失败关闭。helper 会补齐主体样式区间、按边界切分并合并相邻同样式 Run，因此输出始终连续覆盖全文。

页面脚本先按本文件“固定框安全区”算出最终 `source_bbox/slide_bbox/margins`，再调用 helper。`vertical_alignment` 与 `text_safety` 都是无默认值的 keyword-only 参数；`text_safety` 只验证调用点已经显式分类，不序列化。返回的两个 dict 直接放入 `elements[]` 与 `modules.typography.items[]`，禁止把 `paragraphs_text/spans/text_safety` 写入规格。

```python
element, typography = compile_textbox(
    element_id="kpi", paragraphs_text=["收入 25%", "同比提升"],
    spans=[{"text": "25%", "font_weight": 700, "color": "#E46C0A"}],
    paragraphs=paragraph_contracts,
    source_bbox=final_source_bbox, slide_bbox=map_bbox(final_source_bbox), layer=3,
    selected_font=PREFERRED_FONT, source_font_guess="unknown",
    fallback_reason="source_font_uncertain", fallback_trace=None,
    font_declaration_verified=False, base_run_style=base_run_style,
    margins=final_margins, alignment="left", wrap=True, overflow=False,
    vertical_alignment="middle", text_safety="container_bound",
    source_layout=source_layout,
)
```

### Text Run

`runs[]` 使用 `[start,end)` 索引并无重叠、无缺口地覆盖完整 `text`。每个 run 必须写 `start/end/font_size/font_weight/color/letter_spacing/italic/underline/strike/baseline`。多色标题、粗体数字和局部样式差异都在同一 TextBox 的连续 run 中表达，不得拆框。

`validate_pptx.py --spec` 按语义区间核对样式；物理 Run 的合并或拆分不改变结果。字号与字距归一到百分之一磅，颜色为大写 `#RRGGBB`，字重按 bold 归一。样式偏差写 `TEXT_RUN_STYLE_MISMATCH` warning；文本缺失、TextBox 歧义、run 重叠/缺口或覆盖不完整仍失败关闭。

### Paragraph 与列表

`paragraphs[]` 每段写 `start/end/alignment/line_spacing/space_before/space_after/indent/list`；原生列表还写 `margin_left`。自动折行不拆 Paragraph，真实段落才拆；`text_box.paragraph_breaks` 等于前面各段的 end，`soft_breaks` 只记录真实软换行。

一个 Paragraph 自动折成多行时保持连续 `text`、`wrap=true` 且不写 `soft_breaks`，只用该段 `line_spacing` 控制行距。两个独立段落必须是两个 `paragraphs[]`；段间距只由相邻一侧的 `space_after` 或 `space_before` 承担，另一侧保持 0，禁止用空段、硬回车或扩大段内行距模拟段距。

视觉上有两行以上，或 TextBox 垂直对齐不是 `top` 时，必须写来源测量目标：

```json
"source_layout": {
  "line_center_distances_pt": [16.2],
  "text_block_center_offset_y_pt": 0.0
}
```

数组按可见行从上到下记录相邻行中心距；空数组表示单行。中心偏移为可见文字块中心减 TextBox 中心，正值向下。`source_layout` 只保存来源目标，不替代 Paragraph、`vertical_alignment` 或 margins。修正顺序为：先锁定 Paragraph 与换行，再校准字号和 box/margin/wrap，随后调字距、`line_spacing`、相邻一侧段距，最后校准文字块纵向中心；不得通过拆框、硬换行或空段掩盖误差。

同源列表只用一个 TextBox，每项一个原生 Paragraph。`list` 使用 `buChar|buAutoNum|buBlip` 对应的 `bullet_type/bullet`，保存 level、字体、字号模式、颜色及 EMU margin/indent。图片 bullet 的 `bullet_asset` 必须为绝对本地 PNG/JPEG，绑定 SHA-256 与像素尺寸；同素材复用一个 media part。素材或 relationship 异常时失败关闭，禁止降级为字符 bullet 或独立 Picture。

`follow_text` 分别映射 `bullet_font→buFontTx`、`bullet_size_mode→buSzTx`、`bullet_color→buClrTx`。列表规范化由 compiler 发布事务完成，Skill 不得另跑后处理。

### TextBox

`text_box` 必须写 `x/y/w/h/margins/alignment/vertical_alignment/wrap/overflow/soft_breaks/paragraph_breaks`。文字 helper 必须把 `vertical_alignment` 与 `text_safety` 声明为无默认值的必填 keyword-only 参数；每个调用点按来源显式选择，且不得为 `top|middle|bottom` 设置默认值。`text_safety` 只允许 `free_text|container_bound`，其几何计算只发生在 `prepare_spec.py`；共享 helper 只验证显式选择并接收安全处理后的 bbox/margin，不写入 schema。Paragraph alignment 与 text_box alignment 必须保持一致。

统一 renderer 固定 `MSO_AUTO_SIZE.NONE`。不得新增 `autoFit/auto_size`、写入 `a:spAutoFit`，或通过收缩框高、移动 y、插入空行和过度缩字伪造居中。

## 首次构建的固定框安全区

首次构建不得把可预防的文字裁切留到 preview。普通视图裁切、仅悬停或双击编辑态显示完整，仍是固定 TextBox 容量不足，不能判通过。

先按来源边界选择作者期分类：

| `text_safety` | 适用对象 | 首次构建动作 |
|---|---|---|
| `free_text` | 无填充、无边框、`wrap=false`，且外侧存在已确认空白的标题、标签或说明 | 自动预留横向 `0.5em`；单行框不足 `1.15em` 时在已确认上下空白内扩高 |
| `container_bound` | 卡片、表格、矩阵、节点或其他明确容器内文字 | 不越界扩框；先校正来源 bbox、字号 mapping、margin/wrap 与行段距，字号仅作最后兜底 |

`free_text` 的横向安全量为：

```text
p_pt = max_run_font_size_pt × 0.5
p_source_px = p_pt / scale_pt_per_source_px
p_emu = p_pt × 12700
```

在 source 坐标先做 `x'=x-p_source_px`、`w'=w+2p_source_px`，再用既有 mapping 生成 slide bbox。为保持原文字锚点：left alignment 把 `p_emu` 加到 left margin，center 不补 margin，right alignment 把 `p_emu` 加到 right margin；不得靠改变文字对齐掩盖位移。`container_bound` 不使用这一扩框规则。

对 `free_text` 的单行固定框计算：

```text
h_pt = h_source_px × scale_pt_per_source_px
h_min_pt = max_run_font_size_pt × 1.15
d_source_px = max(0, h_min_pt - h_pt) / scale_pt_per_source_px
```

只有上下存在已确认空白时才扩高：`top` 固定顶边，`middle` 将 y 上移 `d_source_px/2`，`bottom` 将 y 上移 `d_source_px`，同时把 h 增加 `d_source_px`。这是保持垂直锚点的 bbox 补偿；禁止只移动 y 代替增加容量。`container_bound` 无法扩高时，先校正来源字号和段落参数，最后才做最小字号修正。

发现首尾裁切时，先检查 box → margin/wrap；外侧仍有空白时最多提高到 `1.0em`。受限容器没有空间时，字号才是最后兜底。不得硬换行、拆框、改写或图片化掩盖问题。

页面脚本的 `text_safety` 分支只做上述确定性几何处理。不得新增字体文件探测、glyph 宽度测量、候选字体试排、自动字号搜索或文字占用率交付门禁；这些近似计算不能替代 PowerPoint 实际渲染。若 `free_text` 未执行适用的安全区、单行框低于 `1.15em`，或 `container_bound` 越界，只记录并优先修正该文字框，不新增 schema 字段、第二套 IR 或字体测量脚本。

## 字体与字号

每页第一项 `selected_font` 是 `preferred_font`。同页 `selected_font`、`internal_font_declaration`、非空且非 `follow_text` 的 bullet/font 声明必须一致；compiler 写入 `a:latin/a:ea/a:cs/a:sym`，不依赖主题字体。

`source_font_guess` 只记录观感，不确定时写 `fallback_reason=source_font_uncertain`。两种 profile 都不以字体文件、TTC face、fontconfig 或 `pdffonts` 作为构建门禁；字体 fallback 不回写规格、不换字、不触发重建，只有其造成的可见裁切、换行、溢出或层级差异才评级。

字号单位为 pt，文本坐标为 EMU。初值使用页面实际 mapping：

```text
scale_pt_per_source_px =
  min(slide_width_emu / 12700 / page_frame_width_px,
      slide_height_emu / 12700 / page_frame_height_px)
```

比例只映射物理长度，不把 glyph 高度或 OCR 紧框高度当字体 em。`prepare_spec.py` 必须先计算一次当前页面的 `scale_pt_per_source_px`，所有初始字号、框高和行段距都以该 mapping 为依据；不得先凭 glyph 高度硬编码 pt 再用扩框掩盖。按根因依次调整字号、box、margin/wrap、字距、行/段距和垂直对齐；先锁定换行，再校准行中心距和文字块纵向中心。不做候选字体试排、自动字号搜索或阶段性换字。

## 特殊文本与最低可编辑性

特殊文字仍以 `kind=text` 构建，并在 `modules.special_text` 记录额外语义。rotation 在 prebuild 前规范到 `[0,360)`。只有确实无法原生还原且不属于主要文字时，才图片化最小字形。

主要文字、数字、表格数据和基础结构必须可独立选择。复杂图形中的主要标签不能无损分离时，按所需可编辑性失败关闭；不得在含原文字的 picture 上再覆盖一份可编辑文字。
