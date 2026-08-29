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

## 固定文本框的字形安全区

普通视图中部分文字被裁切、仅在鼠标悬停或双击编辑态显示完整时，文字内容和 Text Run 仍然存在；按固定 TextBox 容量不足处理，不拆框、不补字、不图片化。首次构建前必须预防可判断的裁切：以框内最大 Text Run 字号为 `1em`；来源可见字形到任一水平可用边界小于 `0.5em` 时判为高风险，优先检查 `wrap=false` 的多 Text Run、粗体和长单行文字。不得把无可见边框的 TextBox 收紧到字形边界。

### 首次最小安全扩框

先把高风险 TextBox 分为自由文本和受限文本。自由文本必须同时满足：无填充、无边框、`wrap=false`，且扩展方向存在已确认空白。首次固定使用 `p=0.5em`：`p_pt=max_run_font_size_pt×0.5`、`p_source_px=p_pt/scale_pt_per_source_px`、`p_emu=p_pt×12700`；复用页面既有 mapping，不使用固定 DPI，不调用 `TextRange2`、字体 API 或自动测字，不运行碰撞搜索或额外 preview。

设来源 TextBox 为 `x,w`，typography 左右 margins 为 `ml,mr`。先在 source 坐标按 alignment 扩展，再由既有 mapping 生成 element `slide_bbox`；首次容量扩展不修改 margins：

| alignment | 空白条件 | source 变换 | margins | 必须保持的结果 |
|---|---|---|---|---|
| `left` | 右侧 `≥p_source_px` | `x'=x; w'=w+p_source_px` | `ml'=ml; mr'=mr` | 文字起点不变，可用宽度增加 `p_emu` |
| `right` | 左侧 `≥p_source_px` | `x'=x-p_source_px; w'=w+p_source_px` | `ml'=ml; mr'=mr` | 文字右锚点不变，可用宽度增加 `p_emu` |
| `center` | 左右各 `≥p_source_px` | `x'=x-p_source_px; w'=w+2p_source_px` | `ml'=ml; mr'=mr` | 可用文本区中心不变，可用宽度增加 `2p_emu` |

`justify|distributed` 不使用自由文本公式，进入受限文本分支。以 slide EMU 计算 `usable_width=slide_width-ml-mr`；变换后 `left|right` 必须满足 `usable_width'-usable_width≥p_emu`，`center` 必须满足 `≥2p_emu`。不满足说明新增空间被 margin 抵消，禁止进入 build。element `source_bbox` 必须写扩展后的 source 框，`kind=text` element 的 `slide_bbox` 必须由该框按 canvas mapping 生成并与 typography `text_box` 完全同步；原始字形/来源测量框继续保留在 measurement evidence，不得造成 `SPEC_SLIDE_BBOX_MAPPING_INVALID`。

如果容量已经足够、只有对齐锚点一侧的字形 overhang 被外框裁切，则只在该侧扩展外框并给同侧 margin 增加等量 `p_emu`，保持文字锚点和 `usable_width` 不变；不得把这条 overhang 规则用于末字容量不足。所有扩框只进入已确认空白，不改 y/h、字号、字体、字距、Paragraph、Text Run、换行或垂直对齐。

页面边界、表格单元格、矩阵格子、卡片标签及其他受限容器不能提供完整 `0.5em` 空白时，不越过来源容器。按下列固定顺序处理：先调整 box；再只回收不承担视觉锚点的 margin（`left` 只动右侧、`right` 只动左侧、`center` 左右等量）；来源本来允许自动换行时才调整 wrap；仍不足且生成字形确实比来源偏大时，才按下一节公式校准同一语义组字号。不得清零全部 margins。

首次 preview 只在普通视图验证残余首尾裁切、错误换行和扩框重叠；悬停或双击后显示完整不能算通过。若仍有裁切，把全部文字问题合入唯一一次集中修复：外侧仍有空白时沿用相同 alignment 公式，把对应方向的安全区提高到最多 `1.0em`；受限容器继续执行同一固定顺序。

缩小字号不是裁切的默认修复。只有固定框受硬边界约束、box/margin/wrap 均不能在不破坏来源布局的前提下容纳文字，并且生成字形确实比来源偏大时，才以 `new_font_pt=current_font_pt×target_glyph_px/current_glyph_px` 校准同一语义组；不得逐框“减 1pt”试排。若字号与来源一致而只是首尾裁切，继续修 box/margin，不以缩字掩盖容量不足。

统一 renderer 继续使用 `MSO_AUTO_SIZE.NONE`。不得新增页面级 `autoFit/auto_size`、不得在构建后写入 `a:spAutoFit`，也不得把 PowerPoint 的“根据文字调整形状大小”作为常规生成修复。当前 schema/compiler 的单个 `overflow` 同时控制水平与垂直 overflow；文字裁切修复必须保持规格原值，不得把 `overflow=true` 或直接写 `horzOverflow|vertOverflow=overflow` 当作保险。只有固定框无法在不破坏来源布局的前提下容纳文字、且用户另行授权扩展 schema/compiler 能力时，才单独评估 AutoFit 或分轴 overflow；这不属于当前页面的 preview 后补丁。

### 裁切处理速查

| 症状 | 一线修复 | 仍失败时 | 不要做 |
|---|---|---|---|
| 自由文本末端容量不足 | 按 alignment 扩展非锚点方向并重算 mapping | 同方向安全区最多提高到 `1.0em` | overflow、测字、改字号 |
| 锚点侧 glyph overhang | 外框与同侧 margin 等量扩展 | 合入唯一集中修复复核 | 把 overhang 当容量不足 |
| 受限容器容量不足 | box → 非锚点 margin → 来源允许的 wrap | 实测字形偏大时按比例校准语义组字号 | 清零全部 margin、逐框减 `1pt` |
| 编辑态完整、普通视图裁切 | 按固定框容量问题处理 | 普通视图 preview 判定 | 以悬停/双击态判通过 |

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

调整顺序由根因决定：同一语义组的生成字形系统性大于来源时先校准字号；字号与来源一致而首尾裁切时按 box → margin/wrap 处理；受限容器没有扩框空间时，字号才是最后兜底。随后才调整字距 → 行/段距 → 垂直对齐；先锁定换行位置，再校准相邻行中心距，最后校准文字块纵向中心。自动折行只改 `line_spacing`，真实段落优先改相邻一侧的 `space_after/space_before`。不用硬换行、拆框、过度缩字、改写或图片化掩盖问题。若生成了预览，把 source/preview 中全部可见文字问题合入一次修复批次；修正后重跑 build、structure、background，并至多再生成一次预览。字体 fallback 只披露，不修复。`validate_pptx.py --spec` 继续核对 OOXML Text Run 字号与规格 point 值。

## 特殊文本与最低可编辑性

特殊文本写入 `modules.special_text`，优先原生。新任务 element rotation 在 prebuild 前规范到 `[0,360)`，如 `-25→335`；legacy final 可兼容负角。确实无法原生还原时才图片化最小字形。

文字、数字、表格数据和基础结构须可独立选择；照片与复杂装饰只覆盖最小范围。最终检查选择粒度、Text Run、Paragraph、bullet 和图片化风险。
