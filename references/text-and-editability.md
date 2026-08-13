# 文字与可编辑性

## 来源文本容器

内容逐字服从 `content_reference`。一个来源容器对应一个 `TextBox/TextFrame`；不按视觉行拆框，自动折行不写硬回车。`paragraphs[]`/`paragraph_breaks[]` 已表达边界时，`text` 禁用 CR/LF 重复表达；保留真实 Paragraph。

`modules.typography.items[]` 用唯一 `element_id` 绑定文字，保存 text、runs、paragraphs、TextBox 与字体声明；每项只有一个 `selected_font`，runs/paragraphs 连续覆盖全文，坐标用 EMU。生成前不写最终 OOXML ID。

所有 `kind=text` 对象只交给统一 Text renderer；不得另写页面级 TextBox 生成函数。renderer 从 typography 索引取得 Text Run、段落、边距、对齐、wrap 与 overflow，规格缺项或绑定冲突即 fail closed。

## 文字转录与 spans 编写

先确认完整文字、真实 Paragraph 和标点，再处理颜色、字重与局部字号。一个来源 TextBox 对应一次 `add_text()`；自动换行不拆字符串，只有真实 Paragraph 才使用 `list[str]`。数字、百分号、单位、正负号、括号、空格和中英文标点逐字服从来源；模糊字符先检查图片局部，不根据上下文补造。

把框内占比最大的样式作为 `add_text()` 默认值；单一样式 TextBox 不写 `spans`。`spans` 只写差异，按以下顺序定位：唯一文本；多个唯一值使用列表推导式；重复短文本用更长上下文计算显式 `start/end`；只有上下文也无法区分时才使用 `occurrence`。

```python
spans = [
    {"text": "过保替换：", "font_weight": 700},
    *[{"text": value, "color": ORANGE} for value in ("9,312", "4,497", "847", "428")],
]
```

```python
start = text.index("0故障")
spans = [{"start": start, "end": start + 1, "color": ORANGE}]
```

不得手工展开完整 `runs[]`，不得使用正则或“所有数字”等内容类别批量推断样式，也不得通过拆框或硬换行规避 Text Run、Paragraph 或排版问题。

## 行距、段距与框内垂直位置

先按来源语义确定 Paragraph，再调框内排版。一个 Paragraph 自动折成多行时保持连续 text、`wrap=true` 且不写 `soft_breaks`，只用该段 `line_spacing` 控制行距；两个独立段落必须是两个 `paragraphs[]`，段间距只由相邻一侧的 `space_after` 或 `space_before` 承担，另一侧为 0，禁止用空段、硬回车或扩大段内行距模拟段距。

`line_spacing` 沿用 schema v2 的现有比例值，不新增固定磅值模式或平行字段。视觉上包含两行及以上，或来源不是顶部对齐时，该 typography item 必须增加：

```json
"source_layout": {
  "line_center_distances_pt": [16.2],
  "text_block_center_offset_y_pt": 0.0
}
```

数组按可见行从上到下记录相邻行中心距；空数组表示单行。中心偏移是可见文字块中心减 TextBox 中心，正值向下。它只保存来源测量目标，不替代 `paragraphs[]`、`text_box.vertical_alignment` 或 margins。

`text_box.vertical_alignment` 必须服从来源的 `top|middle|bottom`，不得统一设为 `middle`。来源居中时写 `middle` 并保留实测上下 margins；来源上下留白对称时 margins 也对称。统一 renderer 已固定 `MSO_AUTO_SIZE.NONE`，规格不得新增 `autoFit/auto_size` 字段，也不得通过收缩框高、移动 y、插入空行或缩小字号伪造居中。

页面文字辅助函数必须把 `vertical`/`valign` 声明为必填 keyword-only 参数，不得为 `top|middle|bottom` 设置默认值；每个调用点必须根据来源显式传入垂直对齐。该规则只防止遗漏选择，不改变来源本来就是顶部或底部对齐的 TextBox。

## Text Run 与原生列表

字体、字号、字重、颜色、斜体、下划线、删除线、上下标和局部字号变化精确到 Text Run；标题、标签和强调范围不得退化为整框样式，Paragraph 与 Run 不互相替代。

`validate_pptx.py --spec` 按 `element_id` 和 `[start,end)` 语义区间核对 `font_size`、`font_weight`、`color`、`italic`、`underline`、`strike`、`baseline`、`letter_spacing`；物理 Run 的合并或拆分不改变结果。字号与字距统一到百分之一磅、颜色统一为大写 `#RRGGBB`、字重统一为是否 bold。样式偏差写入 `TEXT_RUN_STYLE_MISMATCH` 结构化 warning，不改变 `valid`；TextBox 缺失/歧义、文本不一致以及 Run 重叠、缺口或覆盖不完整仍 fail closed。warning 严重度不随 profile 改变。

同源列表只用一个 TextBox，每项一个原生 Paragraph；bullet 只用 `buChar`、`buAutoNum` 或 `buBlip`。每段保存身份、层级、样式及 EMU `margin_left/indent`，最终由 `validate_pptx.py --spec` 核对。

图片项目符号使用 `bullet_type=picture`、`bullet=blip` 和 `bullet_asset`；素材必须是绝对路径的本地 PNG/JPEG，并携带精确的 SHA-256 与像素尺寸。同一 TextBox 的多个 Paragraph 共用相同素材时，compiler 写入真实 `a:buBlip/a:blip@r:embed` 并按素材身份复用一个 media part。素材或 relationship 异常时 fail closed，禁止降级成字符 bullet 或独立 Picture；validator 同时核对内部 image relationship 与 media SHA-256。

`follow_text`：`bullet_font`→`buFontTx`、`bullet_size_mode`→`buSzTx`、`bullet_color`→`buClrTx`；禁止固化为当前字体、字号或颜色快照。

原生列表的 `buFontTx/buSzTx/buClrTx` 规范化由 compiler 发布事务内部完成；Skill 不得在 compiler 前后另跑列表规范化或继续使用未规范化 PPTX。

## 字体与字号

`rapid` 与 `reviewed` 都把字体视为构建意图，而不是运行时门禁。每页第一项 typography 的 `selected_font` 是 `preferred_font`；同页每个 `selected_font`、`internal_font_declaration`、非空且非 `follow_text` 的 `bullet_font`，以及 element 中的 `font_name/font.name` 都必须与它一致。`source_font_guess` 只记录源图观感，来源不确定时可写 `fallback_reason=source_font_uncertain`。

compiler 必须把 `preferred_font` 显式写入一致的 `a:latin/a:ea/a:cs/a:sym`，不得依赖主题字体。两种模式都不检查字体文件、TTC face 或 fontconfig；LibreOffice/PowerPoint 的实际 fallback 不回写规格、不触发换字或第二次 build。可选预览中的 `pdffonts` 只生成 `matched/mismatches` 诊断；若 preview 已成功生成，`matched=false` 或 fallback 不使 preview 失效，也不阻止主代理视觉审查。字体 fallback 本身不是 P0/P1，只有导致可见裁切、错误换行、溢出或层级问题时，才按这些可见差异评级。

`runs[].font_size` 固定使用 point（pt），文本坐标使用 EMU；自定义字号字段以 `_font_size_pt` 结尾。初值按页面实际比例估算，不使用固定 96 DPI：

```text
scale_pt_per_source_px =
  min(slide_width_emu / 12700 / page_frame_width_px,
      slide_height_emu / 12700 / page_frame_height_px)
```

比例只映射物理长度，不把 glyph 高度当作字体 em。先确认页面映射、`preferred_font`、显式 margin 和关闭 AutoFit，再按来源估计字号。可选预览无明显字号、换行或溢出差异时继续；有系统性差异时，从标题、正文、数字/KPI、列表/表格等实际存在组别各选一个代表性高风险 TextBox，以 `new_font_pt = current_font_pt × target_glyph_px / current_glyph_px` 修正同一规格，目标框及相邻边界改善后应用于同组。不逐框试排，不做自动字号搜索，不新增字体优化状态机。

不做字体比较、候选字体试排或阶段性换字。特殊字符、生僻字、公式、多语言或缺字只能触发局部内容调查，不能在渲染后修改 `preferred_font`。

调整顺序：字号 → box → margin/wrap → 字距 → 行/段距 → 垂直对齐；先锁定换行位置，再校准相邻行中心距，最后校准文字块纵向中心。自动折行只改 `line_spacing`，真实段落优先改相邻一侧的 `space_after/space_before`。不用硬换行、拆框、过度缩字、改写或图片化掩盖问题。若生成了预览，把 source/preview 中全部可见文字问题合入一次修复批次；修正后重跑 build、structure、background，并至多再生成一次预览。字体 fallback 只披露，不修复。`validate_pptx.py --spec` 继续核对 OOXML Text Run 字号与规格 point 值。

## 特殊文本与最低可编辑性

特殊文本写入 `modules.special_text`，优先原生。新任务 element rotation 在 prebuild 前规范到 `[0,360)`，如 `-25→335`；legacy final 可兼容负角。确实无法原生还原时才图片化最小字形。

文字、数字、表格数据和基础结构须可独立选择；照片与复杂装饰只覆盖最小范围。最终检查选择粒度、Text Run、Paragraph、bullet 和图片化风险。
