# 图片与图标

本文件负责来源资产、背景、图标角色与图标族、裁切和 picture 质量。素材只来自当前页 `clean_visual_reference` 或用户提供的当页原始素材；不得联网、调用 imagegen 生成替代素材、借用其他页资产，或把主要标题/标签并入图片。

## Picture 资产

`render_mode=picture_asset` 必须绑定恰好一个 `kind=picture|icon` element 和绝对、非 symlink 的本地 PNG/JPEG/WEBP，记录 SHA-256 与像素尺寸；element 与 representation item 的 `source_bbox` 必须完全一致。compiler 不自动生成、扩大、替换资产，也不把失败静默改成图片模式。

- icon、pictogram、Logo、照片、插画和纹理都必须来自来源像素，使用 `fallback_policy=required_source_asset`。
- 其他复杂装饰可在 `required_editability=labels_only|none` 时使用 `allow_minimal_asset`；`labels_only` 还须绑定独立可编辑标签。
- 近整页前景 asset 永远拒绝；主要文字、数字和数据必须独立原生。

## 背景

背景仍使用独立 `modules.background.items[]` 合同，其 `selected_mode` 只允许 `native|background_picture`，不属于 representation plan 的 `render_mode`。只有不含任何前景语义的 `clean_background_asset` 才可用 `background_picture`；必须覆盖全页、`mode=none`、crop 全零、opacity=1、rotation=0。不得直接使用含前景的原始整页图。

图标不得绑定为 background，图标裁块也不得充当 `clean_background_asset`。structure 通过后运行 `validate_background_contract.py` 重算 build/OOXML 闭包；未声明满页 picture、前景污染或身份不一致均失败。

## 图标角色判定

真正图标必须从当前页视觉参考裁切为各自独立 picture，并放回原 bbox。简单或复杂图标都不得用 Shape、字符、重绘 SVG、图标库或多个基础 preset 替代。preset 列表只说明 native 能表达什么，不决定对象是不是图标。

判定顺序：

1. 先划定图标槽位和 pictogram family，再检查单个轮廓。文字旁相同关系、列表/卡片中的重复对齐、相近 bbox/描边/颜色/视觉语言，以及只承担业务语义标识，均是图标上下文证据。对应分别优先写 `classification_basis=text_adjacent_symbol|repeated_icon_slot|standalone_semantic_symbol`。
2. 业务符号一律写 `visual_role=icon|pictogram|logo`、`render_mode=picture_asset`、`fallback_policy=required_source_asset`。一旦进入同一 family，全部成员都走裁切，任何成员近似 `can|cube|ellipse|arc` 也不能脱离同组原生重画。
3. 只有不处于图标上下文、承担容器、流程节点、边界、连接或图示几何职责，且完整轮廓高置信匹配单个受支持 preset 的对象，才进入 native 路径。真实流程节点写 `classification_basis=connector_endpoint_node`，并回指至少一个真实 connector 或节点内标签；`native_shape` 还必须 `structural_boundary=true` 与 `full_contour_match=true`。语义名称不能单独决定对象是图标，语义名称或 preset 相似度也不能单独决定它是节点；例如流程图中的真实数据库节点可使用 `can|flowChartMagneticDisk`。
4. 一个独立符号需要两个以上基础 Shape/Line 才能表达完整业务语义时，默认是 pictogram，必须裁切；不得用 `foldedCorner + ellipse + line`、`ellipse + arc` 或多个 `cube` 拼图标。
5. 编号圆点、标签、bullet、分隔线、容器和流程节点不是图标。角色或轮廓不确定时，独立且不含主要文字的业务符号优先使用最小 picture，不得为提高可编辑率而猜 native。

## 图标族 schema

`modules.icons.families[]` 必须完整声明每个图标族：

- `family_id`：唯一族 ID；
- `expected_count`：来源中应有的成员数；
- `member_fact_ids`：恰好列出全体 `source_fact_id`；
- `required_render_mode=picture_asset`。

每个 `modules.icons.icons[]` 必须写 `source_fact_id/family_id/slot_id`；同族 `slot_id` 唯一，icon 记录的事实集合、`expected_count` 和 `member_fact_ids` 必须完全相等。validator 交叉检查 representation plan：图标事实角色只能是 `icon|pictogram|logo`，并且全员 `picture_asset`。组内出现 native 或模式混用即 `SPEC_ICON_FAMILY_MODE_MISMATCH`，成员缺失即 `SPEC_ICON_FAMILY_INCOMPLETE`。

这套族合同是强制分类门禁：`classification_basis=repeated_icon_slot` 的 `repeat_group_id` 必须解析到该事实所属的唯一 family。不得只在 prose 中写“同组一致”，也不得让单个 preset 相似度覆盖 family 结论。轻量门禁负责角色—模式、事实引用、bbox 与绑定一致性；它不通过新 inventory 独立重做像素盘点。reviewed 的七类审查必须直接对照 source 核实歧义 preset 的关系证据。

## 图标提取

`extract_icon_asset.py` 是唯一生成入口，每次只处理一个已测量图标。一次全页盘点固定全部 `source_bbox=[x,y,w,h]` 后，可并发运行多个独立进程；每个进程使用唯一 `icon_id` 与输出 `assets/icons/<icon-id>.png`，不得共享临时文件。只重跑失败或触边项。

新任务固定 `padding=0`；bbox 本身必须包含完整轮廓、阴影和少量背景，生成器不得再次扩框。前景触边时只允许根据上下文修正 bbox 后再跑一次；第二次仍触边或确认与结构粘连时，停止图标提取，改为不含主要文字的最小局部 picture。

裁切固定 `crop_mode=alpha_isolation`：

- `foreground_profile=standard`：只把与裁切四边 4-connected 连通的同色背景写透明；
- `reverse-white-outline`：仅用于彩色背景上的白色/近白色反白线框图标，并要求 bbox 同时包含完整线框和少量可识别彩色背景。

白底、无彩色边距、白色前景触边或场景不明确时不得使用 `reverse-white-outline`。输出必须为 RGBA PNG，包含透明背景和可见前景，前景不触边；RGB 逐像素等于来源裁块，只允许 alpha 改变。不得重绘、改色、羽化、删除小部件或按面积过滤。

`modules.icons` 绑定当前 visual 的路径/哈希；每个 icon 还必须记录资产路径/哈希、alpha hash、尺寸、layer、`crop_mode`、`padding=0`、`native_redraw=false`、`object_type=picture` 和可选择性状态。最终 PPTX 中嵌入媒体 SHA-256 必须等于当前资产哈希，每个图标可独立选择。

全部最终图标通过自动校验后，只对最终资产集合运行一次 `create_icon_green_preview.py` 并展示 `comparisons/icon-alpha-preview.png`。绿幕图只用于过程可观察性，不是审核门禁，不写入 schema；资产变更后才重新生成。

## 非图标图片

`modules.picture_framing` 记录素材路径/hash、source/slide bbox、原始与显示比例、`contain|cover|none`、crop、焦点/偏移、mask、圆角、rotation、transparency、border/shadow/reflection/glow 和 picture fill。

优先保持来源像素和宽高比；只有来源确为图片填充时才使用 picture fill。`cover` 必须有焦点/偏移证据，不能裁主体或带入邻近文字、图标、边框和线；无平铺证据不得 tile，背景不得出现接缝、漏底或无依据重复。圆形必须保持正圆，不扩图、不补造被裁区域。

mask/alpha 抗锯齿必须连续，不得有白边、黑边、绿幕、色晕、断裂或不透明 halo。border/shadow/reflection/glow 只在来源可见时使用，分别记录方向、距离、blur、透明度、颜色、扩散和层级，不套默认效果，也不用不透明色块遮盖透明边缘。

先以整页核对构图、位置、焦点和层级，再以 200%–300% 局部检查 crop、mask、alpha、圆角、边框和效果。误裁、拉伸、边缘断裂、拼接缝、halo 或效果方向错误均为 P1，不得因“整体相似”放行。
