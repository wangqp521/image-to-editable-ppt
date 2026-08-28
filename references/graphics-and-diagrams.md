# 图形与图示

本文件只负责原生几何、multipart 结构、连接线、表格、矩阵、状态组件和图表。picture/icon 不属于 native；它们由[图片与图标](pictures-and-icons.md)负责。

## 原生表示边界

`render_mode` 的 native 能力是封闭集合：

- `native_text`：普通/特殊文字与主要数字；
- `native_shape`：单个受支持基础 preset；
- `native_line`：直线或可由连接线合同忠实表达的连接；
- `native_table`：规则表格及合并单元格；
- `native_chart`：满足封闭数据合同的简单二维图表；
- `composite_native`：真实由多个几何部件组成的流程结构、节点组、状态组件或重复布局。

先核对 `classification_basis/classification_evidence`，再判断 `visual_role`，最后判断 preset。角色—模式为封闭合同：`text→native_text`，`icon|pictogram|logo|photo|illustration|texture|ornament→picture_asset`，`container→native_shape|composite_native|picture_asset`，`connector→native_line|picture_asset`，`diagram_node→native_shape|composite_native|picture_asset`，`diagram_geometry→native_shape|native_line|composite_native|picture_asset`，`chart→native_chart|picture_asset`。结构角色只有在 native 无法忠实表达时才使用不含主要文字的最小 `picture_asset`，并保持可分离标签原生可编辑。`data` 只允许对应的文字、表格、图表或组合数据结构模式；前景 `background` 不得进入 representation plan。

只有 `container|diagram_node|diagram_geometry` 等结构角色，且完整可见轮廓高置信匹配单个 PowerPoint preset 时，才使用 `native_shape`。`diagram_node/native_shape` 还必须回指真实 connector 或节点内标签，不得仅凭相邻文字、图标槽位或 `can|cube|flowChart*` 轮廓通过。`icon|pictogram|logo` 必须走 `picture_asset`；需要多个基础 preset 才能表达业务语义的独立符号也不是 `composite_native`。

`required_editability=full|labels_and_geometry` 禁止 asset fallback；没有忠实原生表示时 prebuild 失败关闭。`parts/repeat_sequence` 默认禁止重叠；只有来源确有重叠且各 part bbox/层级忠实时，父 element 才可写 `content.allow_overlap=true`。

### 受支持基础 preset

- 矩形：`rectangle|roundRect|round1Rect|round2SameRect|round2DiagRect|snip1Rect|snip2SameRect|snip2DiagRect|snipRoundRect`。
- 基本形状：`ellipse|triangle|rtTriangle|parallelogram|trapezoid|nonIsoscelesTrapezoid|diamond|pentagon|hexagon|heptagon|octagon|decagon|dodecagon|plus|frame|halfFrame|corner|diagStripe|teardrop|chord|pie|pieWedge|donut|arc|blockArc|bracePair|bracketPair|leftBrace|rightBrace|leftBracket|rightBracket|can|cube|bevel|foldedCorner|plaque|noSmoking|smileyFace|heart|lightningBolt|sun|moon|cloud`。
- 简单块箭头：`rightArrow|leftArrow|upArrow|downArrow|leftRightArrow|upDownArrow|quadArrow|chevron|homePlate|notchedRightArrow|stripedRightArrow`。
- 公式、标准流程图、规则星形及简单面状标注仅限 schema 的 `BASIC_NATIVE_SHAPE_TYPES`。

`rectangle` 是兼容别名，最终 OOXML 为 `rect`。`style.adjustments` 一旦声明，必须写齐该 preset 的全部黄色调节点；`flip_horizontal/flip_vertical` 必须显式布尔值。形状类型、adjustments、flip、rotation、fill、line 和 effects 按来源一次写全，不依赖 renderer 默认值。

### Shape/Line 样式与 OOXML 安全

- `shape.style.fill` 只使用 `noFill`、显式 RGB/opacity 的 solid，或至少两个严格递增 stop 的定向 linear gradient；来源无渐变时不得补渐变。
- Shape/Line 的 `style.line` 显式写 `color/width/dash/opacity`；无描边必须是真正 no-line，不能用白线或透明近似。块状箭头匹配实际 preset，只有细连接关系使用 line arrow marker。
- `shape.style.effects` 只使用 `none` 或来源可见且参数完整的 `outer_shadow`。`none` 必须保留 `p:style`、把 `a:effectRef` 设为 `idx=0`，清除 `spPr` 下旧 `effectLst/effectDag` 后写空 `effectLst`；该规则不作用于表格、图片和 `graphicFrame`。
- 所有可见黄色调节点必须一次写全；不得依赖默认 adjustments、用 rotation 伪造 flip，或顺带改变 bbox、fill 和文字位置。

以下不属于基础 preset 路径：自由曲线；弧形、循环、弯折或多段箭头；箭头标注组合；wave/scroll/ribbon；schema 无法精确表达的渐变/纹理；复杂阴影、发光、3D；多层同心环或带业务图标语义的环。基础 Shape 的明确线性渐变可按来源使用原生 gradient fill；不得用相近 preset、bounding ellipse 或多个基础 Shape 近似复杂轮廓。

复杂图形独立可分离时使用最小 `picture_asset`；与节点、连线或圆环粘连时，picture 只保留复杂轮廓及维持视觉连续所需的最短连接段。轮廓外可明确分离的标准节点、直线段和标签仍原生重建。若拆分会截断轮廓或形成重复边缘，则使用最小完整黏连子图，禁止在下方重复绘制同一节点或线。

标签 bbox 可从资产排除时必须排除并原生重建；标签位于复杂轮廓内部且无法无损分离时，`required_editability=full|labels_and_geometry|labels_only` 均须 prebuild 失败。不得把原文字留在图片后再叠加一份可编辑文字。

## 表格、矩阵与状态组件

行列和合并明确时使用 `native_table`；不规则分区、边界、组或跨行关系使用 Shape/Line/TextBox。表格不拆成文本框，网格图示不强制表格化。

表格记录行列、merge、row/column span、每格文字、paragraph/run、margin、对齐、填充、四边框、层级和元素身份。无边框必须是真正 no-line，不能用白线。每格文字只生成一次，避免重复段落或图层。

矩阵和状态组件使用 `composite_native`，保存 `part_defaults/parts/repeat_sequence`。每个 part 显式写 `part_id/part_kind/source_bbox/slide_bbox/layer/style/content`；可见样式不得依赖默认值。

状态条必须区分来源结构：底轨与填充同高且底轨真实可见时使用 `track_plus_fill`；只有填充后接细线时使用 `fill_plus_continuation_line` 且 `track_bbox=null`。逐例保存 track/fill/continuation 三类 bbox、中心线、端点、比例与层级；续线从 fill 右端开始。不得补造底轨、统一不同长度或让相邻条跨接。

## 连接线与图示

连接线记录精确端点、方向、宽度、颜色、dash、head/tail、层级及连接关系。不要把 `x/y/w/h` 当视觉中心；检查端点是否触达正确对象、是否穿越文字、箭头头型和方向是否一致。交叉仅在来源存在时保留。

图示关系按 `source_node+port → route/bend_points → target_port+node` 保存。端点附着对象边界，多段线相邻端点必须重合；不得悬空、接错节点、穿过节点或标签、截断路径，也不得为简化 renderer 而拆分或合并来源关系。重复卡片、KPI 和步骤共享来源中的尺寸、padding、圆角、基线与间距，只记录真实例外；不自动等距，Group 不包入无关对象，文字和 connector 保持独立可编辑。

图示先确定节点 bbox、层级和关系，再生成连接线，最后放置标签。主流程、回流、分支、反馈和闭环不能漏。标准节点/连接使用 native；复杂装饰进入最小 picture。图标槽位中的语义符号不因与流程相邻而变成节点。

## 图表

`native_chart` 仅支持证据充分的二维 `pie|doughnut|column|bar|line`。来源类型、分类、系列、数值、顺序、颜色及所有启用样式都必须确认；renderer 必须生成 `graphicFrame`、chart part 与 embedded workbook，不能静默 fallback，也不能把图表烘焙为图片。

### 饼图与环形图

- `slices[]` 按视觉顺序逐块写 `category/value/color/value_source`；pie/doughnut 只有一个 series。
- `derived_complement` 只用于两块扇区且来源明确整体为 100% 的 `100-x`，不得推断分类名、第三块数据或一般归一化。
- `first_slice_angle`、doughnut 的 `hole_size`、逐块颜色和 `data_labels` 按来源显式写入；扇区顺序、起始角度、孔径、标签内容与位置都必须核对。

### 柱、条与折线

- `categories[]` 和每个 `series.values[]` 等长；column/bar 不允许 null，line 的 null 保持真实断口，`display_blanks_as=gap`，`smooth=false`。
- column/bar 只允许 `clustered|stacked|percent_stacked`；stacked 与 percent_stacked 的 `overlap=100`，并按来源写 `gap_width`。line 使用 `grouping=standard`，逐系列写线宽、dash、marker 和颜色。
- `axes.category/value` 显式写方向、位置、可见性、逆序、标签位置、字体与轴线；数值轴再写 `minimum/maximum/major_unit/number_format/major_gridlines`。不得因图面模糊而补造轴范围或刻度。
- `legend`、`data_labels`、`chart_area` 与 `plot_area` 只在来源启用时按合同写入。标签的 category/series/value/percentage 开关、位置、格式、字号、字重和颜色不能依赖 PowerPoint 默认值。

### 图表裁切与 fallback

native chart 没有 crop 字段，也不支持 `a:srcRect`；它的 `slide_bbox` 就是完整图表框，禁止通过扩大图表再遮罩或伪造裁切。若来源图表本身只显示局部、需要裁切，或属于 3D、组合/双轴、散点/面积/瀑布、趋势线、误差线、渐变/纹理系列、逐点复杂格式、平滑曲线、复杂阴影等超出合同的类型，则使用最小 `picture_asset`。picture fallback 通过 `content.mode/crop` 进入通用图片 renderer，并在 OOXML 写入 `a:srcRect`；标题、单位、中心 KPI、图例说明和外围注释中可分离的文字仍独立原生可编辑。

构建后必须核对 chart part、embedded workbook、plot 类型、系列/分类 cache、系列顺序、轴 ID 与交叉关系、刻度、gridline、图例/标签开关、颜色、gap/overlap、角度/孔径和缺失值断口。折线不得跨断口、平滑、越界或改极值；柱条核对方向、基线、gap/overlap 及分组/堆积；饼环核对扇区顺序、起始角度和内径。picture fallback 则核对素材哈希、像素尺寸、`a:srcRect`、bbox、rotation、opacity 与层级。两条路径都不得造数据、分类、系列、轴或趋势。
