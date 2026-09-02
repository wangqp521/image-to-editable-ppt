# 元素表达分类

本文件是每个非空页面的唯一元素分类真源。先完成全页元素与关系盘点，再对每个非背景元素按本文件固定 representation，随后才生成图标/图片资产和写入 schema v2。背景不属于本分类，继续按 background contract 判定。

`native_editable` 指内容级原生可编辑；`selectable_picture` 指可在 PowerPoint 中整体选择、移动、缩放和裁剪，但内部像素不可编辑。这两个名称只是分类术语，不是 schema 字段、representation mode 或新元素类型。

## 固定判定顺序

分类单位是能够独立表达、独立定位或独立选择的最小语义元素。不得为减少对象数量而把整张卡片、整组图示或包含主要文字的区域合并为图片。对每个元素依次执行以下判定；命中前一项后停止，不得跳步。

### 1. 分离主要文字、数字、标签和数据

普通文字、数字、数据标签、标题、注释和说明文字必须从相邻图形中分离，使用原生 TextBox 或对应原生对象，不得并入图标或局部 picture。图表、插画或复杂装饰即使使用 picture，其标题、单位、中心数值、外围标签和主要说明仍须单独保持原生可编辑。

只有来源本身是不可分离的艺术字，且 `required_editability=none` 时，才允许将该艺术字保留在最小局部 picture 中。

### 2. 识别已有图片素材

照片、Logo、截图、纹理和已有透明图片直接使用普通 `kind=picture`，分类为 `selectable_picture`；不得使用 Shape、字符、重绘 SVG、图标库或 `extract_icon_asset.py` 替代。Logo 内的品牌文字属于 Logo 资产本体，不拆成 TextBox。

该 picture 只保留来源内容所需的最小局部区域。满页照片或纯纹理只有满足 background contract 时才能作为背景；含主要前景语义的整页图片不得作为背景或普通元素交付。

### 3. 识别独立图标

只有同时满足以下全部条件的元素才是独立图标：

- 是表达对象、功能、状态或动作的独立 pictogram；
- 具有相对完整、自包含的视觉边界；
- 能在不包含邻近文字、编号、容器或连接关系的情况下单独裁切；
- 不是照片、Logo、截图、文字、数字、bullet、普通圆点、分隔线、边框、背景纹理、容器、连接线、流程箭头或纯布局装饰。

独立图标无论视觉上简单或复杂，都分类为 `selectable_picture`，使用 `extract_icon_asset.py` 从当前页视觉参考裁切为独立 picture 并放回原 bbox。一个元素不能仅因为尺寸较小、位于文字旁边、具有装饰作用或能够用基础 Shape 画出，就被认定为图标。

### 4. 判断是否能够准确原生表达

排除已有图片素材和独立图标后，只有同时满足以下全部条件的元素才分类为 `native_editable`：

- 元素类型属于当前明确支持的原生合同；
- 来源几何能够由当前合同直接表达，不需要近似、简化或补造；
- bbox、轮廓、方向、端点、拐点、填充、线型、层级和连接关系均能保持；
- 不需要增加来源中不存在的节点、线段、连接关系或视觉结构；
- 不依赖当前 renderer 尚未实现的 preset、自由曲线、图片效果或图表能力。

当前原生合同仅包括：

- 普通文字、数字和标签；
- `rectangle|roundRect|ellipse|triangle|chevron`；
- 块状、带面积填充且几何匹配的直线型 `rightArrow`；
- 直线和当前合同支持的 connector；
- 表格、矩阵和状态条；
- 数据、分类、系列和样式均可确认的简单二维 `pie|doughnut|column|bar|line` 图表。

“能够拆成若干 Shape”不等于“能够准确原生表达”。只有当每个 part 都对应来源中可辨认的独立视觉部件，且组合后能保持来源的轮廓、位置、重叠和层级时，才允许使用 `composite`。如果拆分会改变来源轮廓、弧度、比例、线宽、连接方式、方向、层级或视觉连续性，则不得为提高编辑性而使用原生 Shape。

### 5. 使用最小局部 picture

不满足原生表达条件、且不属于独立图标的视觉内容，统一分类为 `selectable_picture`，使用最小局部 picture，包括但不限于：

- 插画和当前 Shape/Line/Connector 无法准确表达的复合线稿；
- 自由曲线、长弯箭头、曲线路径和不规则流程箭头；
- 飘带、光效、手绘线条、复杂装饰和艺术字；
- 当前原生合同无法准确表达的其他内容；
- 数据、结构或样式证据不足，无法安全生成的复杂图表主体。

最小局部 picture 只包含无法原生表达的视觉区域；主要文字、数字、数据和标签必须保持为独立原生对象。具体执行只有以下两种子类型：

1. 照片、Logo、截图、纹理、已有透明素材，以及无法稳定去除背景的复杂内容，使用普通 `kind=picture`。
2. 非图标复杂装饰与背景能够通过连通关系稳定分离、foreground seed 能够唯一命中目标、完整目标在合理 bbox 内不触边时，使用 `extract_picture_asset.py` 生成局部透明 picture。

局部透明装饰仍保持 `kind=picture`、`selected_mode=asset`，不得写为 `icon_only`或进入图标绿幕预览。如果透明提取条件不成立，不得增加 AI 分割、颜色分类或页面专用抠图逻辑；应保留能完整覆盖目标的更大最小局部 picture，或明确记录无法高保真分离。

## 可编辑性门禁

只有 `required_editability=labels_only|none` 时才允许最小局部 asset fallback：

- `labels_only`：picture 必须绑定独立原生可编辑标签；
- `none`：允许整个最小视觉元素使用 picture；
- `full|labels_and_geometry`：禁止 asset fallback；当前原生合同无法准确表达时，prebuild 必须失败，不得静默降级。

## 执行速查

| 已命中的类别 | 表达 | 详细合同 |
|---|---|---|
| 主要文字、数字、标签 | `native_editable` | [文字与可编辑性](text-and-editability.md) |
| 已有照片、Logo、截图、纹理 | `selectable_picture` / 普通 picture | [图片与图标](pictures-and-icons.md) |
| 独立图标 | `selectable_picture` / icon | [图片与图标](pictures-and-icons.md) |
| 能准确原生表达的图形、图示、图表 | `native_editable` | [图形与图示](graphics-and-diagrams.md) |
| 其他无法准确原生表达的视觉内容 | `selectable_picture` / 最小局部 picture | [图片与图标](pictures-and-icons.md) |

不得以“更省时”“对象更少”“更容易实现”或“整体看起来相似”为由改变分类顺序或 representation。
