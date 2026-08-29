# 图形与图示

`modules.graphics/diagram/chart` 引用 v2 `element_id`，禁填 OOXML ID；存数/bbox/结构/样式/层级/可编辑性。

当前 native：文字、rectangle/roundRect/ellipse/triangle/chevron/rightArrow、线、表格、matrix/status、picture/icon，以及满足封闭合同的简单 2D `pie|doughnut|column|bar|line`；multipart 用 `composite` parts/repeat，不建 IR。自由曲线、其他 preset 和超出首期合同的复杂图表不原生构建；`required_editability=full|labels_and_geometry` 禁 asset fallback，prebuild 失败即停。`parts/repeat_sequence` 默认禁重叠；仅源图确有重叠且各 part bbox/层级忠实时，父 element `content.allow_overlap=true`；禁为绕错改 bbox、并 parts、滥用开关。

首轮构建时，块状、带面积填充的箭头优先使用 `rightArrow`；细连接关系才使用 line arrow marker。该选择只复用 compiler 已支持表示，不得为规避 renderer 差异改变来源语义、方向或几何。

## 表格、矩阵与框线

行列/合并明确用原生表格；不规则分区/边界/组/跨行用 Shape/Line/TextBox。表格不拆文本框，网格图示不强制表格化。

存行列数、非均匀尺寸、merge span、cell fill/margin/align、四边及线起止/颜色/宽度/透明度/虚实；禁补网格/无线区、延长局部线。合并区、组内外线/填充逐范围存；线不穿合并区，换行不生线。

闭合虚框为对象，存 bbox、线宽、颜色、虚线、层级，不按短线计数或漏边。

## 状态条、圆角、线和填充

底轨同高可见才用 `track_plus_fill`；仅细线续接用 `fill_plus_continuation_line` 且 `track_bbox=null`。每例存三类 bbox、中心线、端点、比例、层级；续线始于 fill 右端。禁伪底轨、统一长度、掩差、跨/并行；长中短均核对。

v1 shape/line 合同（字段齐全、不扩展）：

- `shape.style.fill` 仅：`"noFill"`、`{"type":"solid","color":"#RRGGBB","opacity":0..1}`，或 `{"type":"linear_gradient","angle":0..<360,"stops":[{"position":0..1,"color":"#RRGGBB","opacity":0..1},...]}`；gradient 至少 2 stops，position 严增，仅用于连续定向变色。
- shape/line `style.line`：`{"color":"#RRGGBB","width":12700,"dash":"solid","opacity":1}`；width 为 1..20116800 整数 EMU，dash 仅 `solid|dash|dot|dashDot`；另存起止/端点/层级，遮挡仍错误。
- `shape.style.effects`：`"none"` 或 `{"outer_shadow":{"color":"#RRGGBB","opacity":0..1,"blur_radius":0,"distance":0,"angle":0..<360}}`；半径/距离为非负整数 EMU。`none` 对目标 Shape/Line：保留 `p:style`、令 `a:effectRef idx=0`，清除 `spPr` 下 `effectLst/effectDag` 后写空 `effectLst`；排除表格/图片/`graphicFrame`。
- 矩形用 `rectangle`；圆角/胶囊用 `roundRect`，`style.adjustments` 为 `(0,0.5]` 单值数组，按 preview 校准；禁依赖默认值或顺带改 bbox/填充/文字。

## 图示、Connector 与重复组件

存 nodes/ports/edges/groups/component_templates。edge：`source_node+port → route/bend_points → target_port+node`；端点附边界，路径/拐点/箭头/线型/Z-order 保真，多段端点重合，不悬空、入错节点、穿节点/标签或截断；不乱拆/合并关系。

重复卡片/KPI/步骤共享尺寸/padding/圆角/基线/间距，只留例外；不漂移/自动等距。Group 不包无关项；文字/connector 独立可编辑。

## 图表

简单 2D 图表只有在来源类型、分类、系列、数值、顺序、颜色和全部已启用样式均可确认，且不存在首期非目标能力时，才使用原生 ChartRenderer。Renderer 生成 `graphicFrame + chart part + embedded workbook`，不静默 fallback。`pie|doughnut` 仅单系列；`column|bar` 支持单系列或多系列 `clustered|stacked|percent_stacked`；`line` 支持多系列和 `null` 缺失值断口。

分类、数值、颜色合并写入 `slices[]`；`value_source` 仅为 `explicit|derived_complement`。`derived_complement` 只用于两块扇区、一个明确整体百分比的 `100-x` 补余数，不推断分类名、第三块数据或一般数值归一化。`first_slice_angle` 为 0–359；doughnut 的 `hole_size` 为 10–90；pie 不写孔径。两类图表共用一套数据标签合同；`position=center` 表示扇区中心，圆环中心 KPI 必须另建原生 TextBox。

笛卡尔图表 content 显式写 `chart_type/grouping/categories/series/axes/legend/data_labels/display_blanks_as`。每个系列的 `values[]` 与 `categories[]` 等长；柱条值必须为有限数字，折线值可为有限数字或 `null`，每系列至少一个非空点。`display_blanks_as` 固定为 `gap`，禁止补点、插值、跨缺口连线或平滑。系列 `name` 可为 `null`，颜色为系列级纯色；首期不做逐点异色。

柱条 style 显式写 `gap_width/overlap/chart_area/plot_area`：gap 为 0–500，overlap 为 -100–100，`stacked|percent_stacked` 必须 100。百分比堆积只改变显示，workbook 保留原值；显式百分比轴使用 0..1 与百分比格式。折线系列显式写线宽/虚实、marker 样式/大小/填充/边线和 `smooth=false`；marker 仅 `none|circle|square|diamond|triangle`。

`axes.category` 与 `axes.value` 始终按语义命名，即使 bar 的物理方向互换。保存轴显隐/位置/反向、标签位置、字体/线、value 轴 min/max/major unit/number format 及主网格线；未明确的 min/max/major unit 可为 `null`，其他必填样式不得借 Renderer 默认值猜测。图例保存显隐、top/bottom/left/right、overlay 和字体。笛卡尔数据标签不使用饼环的 `show_percentage`；柱条位置限 center/inside_base/inside_end/outside_end，折线限 above/below/center/left/right。

3D、组合图、双轴/次轴、散点/面积/瀑布、趋势线、误差线、渐变/纹理系列、任意逐点格式、平滑曲线、复杂阴影或证据不足时，继续使用当前最小局部 picture 路径；标题、单位、中心 KPI 和外围注释仍可独立使用 TextBox/Shape。不得在原生 Renderer 内改走图片，也不得造数据、分类、系列、轴或趋势。

图存 type/三类 bbox/表示法/分类与系列序/确认点/轴/刻度/gridline/legend/label/颜色/线型/fill/marker/裁剪。结构验证核对原生 plot 类型、数据 cache、系列顺序与样式、缺失点、轴 ID/交叉关系、刻度、gridline、legend、labels、gap/overlap、角度/孔径和 embedded workbook。折线有序，无断口/突刺/串线，marker 居中；缺失值按源断开，不平滑/越界/改极值。柱条查方向/基线/gap/overlap/分组或堆积；饼环查序/角度/内径。

对象数、merge/边界、状态条、connector 连续性、图表映射/裁剪错误不以“整体相似”放行。
