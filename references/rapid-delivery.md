# Rapid 交付

## 适用范围

本文件定义 rapid 基础链的停止与交付合同。`verification_profile=rapid` 时只读取本文件；reviewed 也先完整执行同一基础链，再进入其专属视觉审查。

## 草稿资格

prebuild、build、structure 和 background 均不传 `--runtime`。structure/background 必须 valid，并各自绑定当前 PPTX 实际 SHA-256；内容完整性、主要内容可编辑性、TextBox/Run、图片化范围和 PPTX 可打开性也必须满足硬门禁。

只有确定性硬门禁失败才允许按共同根因集中修改 `prepare_spec.py` 一次，并从 prebuild 起重建。硬门禁通过后立即停止 producer，进入草稿交付；不得为了进一步优化继续测量、探索或修改页面。

## Preview 诊断

preview 可按需对当前 PPTX 哈希尝试一次：

```bash
python3 scripts/render_preview.py work/page.pptx --preferred-font "Hiragino Sans GB" --output-dir preview/PPTX_SHA256
```

`PPTX_SHA256` 必须替换为当前 PPTX 的实际哈希，preview 文件的直接父目录必须正是该哈希。成功后只确认文件、路径、哈希和诊断报告绑定当前 PPTX；不打开 preview 作 source 对照，不进行视觉判读，不产生视觉 finding，不修改 `prepare_spec.py`，也不触发第二次渲染。

command error、`SIGABRT`、无 PDF/preview 或 Poppler 缺失时，记录 preview 不可用；不 preflight、不重试、不切换 locale/fontconfig、不换字体、不重建。字体 fallback、`pdffonts` mismatch 或 `matched=false` 只记录诊断。

## 交付

硬门禁通过后使用 draft merger：

```bash
python3 scripts/merge_pptx.py --draft --input page-001/work/page.pptx --input page-002/work/page.pptx --output final/deck-draft.pptx
```

draft merger 重新验证输入和合并后 deck 的 PPTX 结构；调用前必须已经确认每页 structure/background 当前哈希绑定。交付可编辑 PPTX、结构/背景报告以及实际存在的 preview/诊断，不得因 preview 缺失或失败扣留草稿。
