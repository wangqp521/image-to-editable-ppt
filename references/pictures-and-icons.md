# 图片与图标

元素必须先按[元素表达分类](element-representation.md)确定为已有图片素材、独立图标或非图标最小局部 picture；本文件只定义这些 Picture 对象的资产生成、绑定与验证合同，不重新分类。

素材只来自当前页 `clean_visual_reference` 或用户原始素材；不得联网、调用 imagegen 生成替代素材、借用其他页资产，或把外部标题/标签并入图片。照片、Logo、插画、纹理和复杂装饰可用独立局部 picture；主要文字、数字和数据仍保持可编辑。满页照片/纹理可作背景，但主要内容不能整页图片化。

只有 representation plan 显式选择 `asset`、`fallback_policy=allow_minimal_asset` 且所需可编辑性为 `labels_only|none` 时，compiler 才接受最小局部 asset fallback；`labels_only` 还须绑定原生可编辑标签。资产必须为绝对、非 symlink 的本地 PNG/JPEG/WEBP，并绑定当前 SHA-256 与像素尺寸；来源 bbox 必须与 picture 完全一致，近整页 asset 永远拒绝。compiler 不自动生成、扩大或替换资产，也不把失败改成 asset 模式。

## 背景选择与后置门禁

可用原生 shape/fill 精确表达、且绑定对象可在 OOXML 中唯一证明时选 `native`。只有已单独制作、不含任何前景语义的 `clean_background_asset` 才可选 `background_picture`；它必须绑定普通 picture，覆盖全页，`mode=none`、四边 crop 全零、opacity=1、rotation=0，不得直接使用含前景的原始整页参考图。`background_picture` 是背景合同专用模式，不加入普通 representation fallback 模式集。

图标对上述选择的影响必须为零：图标数量、bbox、alpha、颜色或层级都不得改变 `native|background_picture` 决策；图标不得绑定为 background，图标裁块也不得充当 `clean_background_asset`。structure 通过后用 `validate_background_contract.py` 重算 build/OOXML 闭包，任何未声明的满页 picture、背景与前景污染或身份不一致都失败；不得手写 `valid=true`。

## 图标

只有按元素表达分类确认的独立图标才能进入本节。图标必须从当前页视觉参考裁切为各自独立 picture，放回原 bbox；简单或复杂图标都不得用 Shape、字符、重绘 SVG 或图标库替代。`extract_icon_asset.py` 是唯一图标资产生成入口，每次只处理一个已测量图标；不得编写页面专用裁切脚本，也不增加对象检测、批处理状态机或第二套资产规格。

先在一次全页盘点中固定全部图标 bbox，再并发启动多个独立 extractor 进程。每个进程只读同一 visual，使用唯一 `icon_id`，写唯一 `assets/icons/<icon-id>.png`，且不共享临时文件或输出目录；并发只缩短等待，不改变单图标合同。只重跑失败或前景触边项，其他已通过资产不得重复提取。

规格坐标统一为 `source_bbox=[x,y,w,h]`。bbox 必须包含完整轮廓、阴影和少量周边背景，大图标和小图标遵守同一规则；新任务固定 `padding=0`，不得在生成器里再次扩框。资产生成前查看带 bbox 的局部上下文，确认只包含目标图标且没有邻近文字、边框或分隔线。任何可见前景触边都视为坐标不足；前景触边时扩大 bbox 后重跑，不得在裁后补边或删除小连通部件。

图标裁切只允许 `alpha_isolation`，固定执行 `alpha_isolation`，不存在第二种裁切模式。`foreground_profile` 只解释前景，取值为默认 `standard` 或显式 `reverse-white-outline`：

- `standard` 保持原行为，只把与裁切四边 4-connected 连通的同色背景写为透明。开放线框内部与外部连通的同色背景默认透明，封闭区域内未与外部连通的底色保持不透明。
- 仅当局部上下文明确显示“彩色背景上的白色/近白色反白镂空线框图标”时，尤其是靶心、放大镜、盾牌等存在闭合孔洞的白色线框，显式使用 `reverse-white-outline`。该配置排除边框中的白色/近白色前景后学习彩色背景，保护白色/近白色像素，并把全裁块内匹配的同源背景写为透明，因此闭合线框内部的彩色底也会透明。

`reverse-white-outline` 的 bbox 必须同时包含完整白色线框和少量可识别的彩色背景边距；白底、无彩色背景边距、白色前景触边或场景不明确时失败，不得猜测、扩大颜色范围或转用该配置。普通彩色/填充图标、白底图标和包含多色语义的图标使用 `standard`；照片和 Logo 不属于图标裁切对象，继续使用普通 `kind=picture`。图标本体、阴影、抗锯齿和所有小部件不做删除、重绘、改色或羽化。输出必须为同时含透明背景和可见前景的 RGBA PNG，前景不得触边，并保存 alpha channel hash。

```bash
python3 scripts/extract_icon_asset.py source.png \
  --icon-id target \
  --bbox-xywh X,Y,W,H \
  --foreground-profile reverse-white-outline \
  --output page/assets/icons/target.png
```

资产尺寸必须等于 bbox 尺寸，RGB 必须逐像素一致于来源裁块；只允许改变 alpha，包括最终透明像素下的 RGB 也不得改变。除 `reverse-white-outline` 上述受限的白色前景保护与同源彩色背景判定外，不得通用阈值抠图、美化、颜色分类过滤或按面积删除连通部件。提取脚本自动检查 RGBA、透明和可见像素、前景边界、RGB 一致性及 alpha hash；任一检查失败即修正 bbox 或输入后重跑，不切换处理模式。

`modules.icons` 绑定当前视觉图路径/哈希，逐 icon 保存唯一 `icon_id/element_id/category`、instance/repeat、semantic_scope、pixel/EMU bbox、layer、source path/hash、固定 `crop_mode=alpha_isolation`、padding/background handling、当前页 `assets/icons` 内非 symlink PNG 路径/hash、alpha hash、尺寸、sharpness、validation、`native_redraw=false`、`selectable_picture_verified` 和 `object_type=picture`。validator 直接校验已声明的 `assets/icons` 路径，不得从 clean visual 父目录推导；实际像素尺寸必须等于声明尺寸，PPTX 嵌入媒体 SHA-256 必须等于当前资产哈希。生成前不写 relationship/object ID；最终每图标必须可独立选择。

当前页全部最终图标生成并通过自动校验后，运行 `create_icon_green_preview.py` 生成 `comparisons/icon-alpha-preview.png`。绿幕背景固定为 `#00FF00`，按 `icon_id` 展示最终 RGBA 资产。通过 commentary 标注 `[第 N/总页数] 图标透明效果展示（仅展示，不设审核门禁）`；每页最终资产集合只展示一次，无图标时不生成、不展示。展示不产生 passed/failed 结论，不等待用户或主代理确认，不作为 prebuild 或 final 证据；绿幕展示不写入 schema。展示后直接进入 prebuild。

图标资产发生变化时，以新的最终资产集合重新展示一次；未变化时不得重复展示。绿幕预览只提供过程可观察性，不得据此引入另一种裁切模式、审核循环、manifest、缓存状态或额外门禁。图标放入 PPTX 后的位置、比例和整页视觉一致性统一由后续结构校验与整页视觉审计处理。

## 非图标图片

普通照片、Logo、截图、纹理和已有透明素材继续作为普通 `kind=picture` 使用，不得进入 `extract_icon_asset.py`。当前 renderer 只实现既有 picture asset、`mode`、四边 crop、rotation 与 opacity；不要在规格中声明尚未实现的 mask、圆角、reflection、glow 或 picture fill 扩展。

### 非图标局部透明 picture

长弯箭头、自由曲线、无法准确原生表达的复合线稿、飘带、光效、手绘线条等非图标视觉内容，如果 representation plan 已选择 `selected_mode=asset`、`fallback_policy=allow_minimal_asset`、`required_editability=labels_only|none`，可从当前页 `clean_visual_reference` 的明确局部 bbox 中提取为透明 PNG。它是“非图标局部透明 picture”；element 保持 `kind=picture`，输出固定写入当页 `assets/pictures/<picture-id>.png`。

`extract_picture_asset.py` 是该类资产的唯一生产入口。bbox 使用来源像素坐标 `[x,y,w,h]`，必须包含目标完整轮廓和少量可学习的周边背景；每个 `--foreground-seed` 是来源坐标中的目标前景点。目标包含多个彼此断开的部件时，为每个需保留的部件重复提供一个 seed。脚本复用图标入口已有的边缘连通背景识别，只保留 seed 所在的 8-connected 前景部件；来源裁块的 RGB 逐像素保持不变，只修改 alpha。

```bash
python3 scripts/extract_picture_asset.py source.png \
  --picture-id curved-arrow \
  --bbox-xywh X,Y,W,H \
  --foreground-seed SX,SY \
  --output page/assets/pictures/curved-arrow.png
```

该入口适合“目标装饰与背景可由连通关系稳定分离”的场景。以下情况直接视为不适用，不增加 ROI、颜色分类、AI 分割或页面专用裁切逻辑：目标与文字/图标真实相交且不可分离；照片、纹理、渐变背景导致边缘背景无法稳定学习；seed 无法唯一命中目标；完整目标在合理 bbox 中仍触边。此时保留更大的最小局部 picture，或明确记录当前无法高保真分离。

每个产物写入 `modules.picture_framing.pictures[]`，并把 `picture_framing` 加入 `activated_modules`；记录 `picture_id/element_id/semantic_role`、source/slide bbox、layer、source path/hash、固定 `crop_mode=alpha_isolation_seeded`、source-coordinate `foreground_seeds`、`assets/pictures` 路径/hash、alpha hash、pixel size、`rgb_preserved=true`、validation、selectability 与 `object_type=picture`。绑定 element 的 asset/path/hash/尺寸必须一致，并使用 `mode=none`、四边 crop 全零。prebuild 会检查来源绑定、RGBA、透明/可见像素、seed 命中、前景不触边、RGB 一致和当前文件 hash；最终阶段再要求该 picture 可独立选择。

局部透明 picture 不生成图标绿幕预览，也不新增独立视觉轮次；位置、比例、层级和整体观感继续由既有整页 preview 与语义视觉判断验证。
