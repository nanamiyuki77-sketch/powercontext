# 端侧文本知识库

在 Chrome / Edge 中导入 UTF-8 的 `.txt` 或 `.md`，用关键词或自然语言搜索。文档与查询的向量都在浏览器生成，seekdb 保存片段并完成混合排序。公开模型下载和推理无需 API Key。

## 启动

需要 Node.js 22.12+、npm、curl 和最新版 Chrome / Edge。在 `stepfun` 中执行：

```sh
npm ci --ignore-scripts
npm run models
npm run dev
```

打开 <http://127.0.0.1:5173>。点击「导入示例」，然后搜索 `WebGPU`。添加自己的文件后，用同一知识库检索；修改知识库名称即可切换。访问 <http://127.0.0.1:5173/?wasm> 可强制使用 WASM。默认只监听本机。

seekdb 默认运行在 Node.js 进程管理的嵌入式模式，数据位于 `.data/seekdb`（可用 `SEEKDB_DATA` 指定目录），首次使用时原生绑定可能需要下载运行库。也可预先启动 seekdb 1.4.0，通过 `SEEKDB_HOST`、`SEEKDB_PORT`、`SEEKDB_USER`、`SEEKDB_PASSWORD`、`SEEKDB_DATABASE` 连接。嵌入式运行库与纯浏览器推理无关。若需构建后启动，先用 `npm run build`，再用 `npm start`。

## 复现实验

保持应用运行，执行 `npm run benchmark`（默认 Edge，环境变量 `BROWSER_CHANNEL=chrome` 可改用 Chrome，`HEADED=1` 可用有界面的浏览器）。也可打开 <http://127.0.0.1:5173/bench.html> 点击运行。脚本先检查增量导入、切换集合、页面刷新后检索以及自动降级，再记录对照实验。复测输出至 `.data/benchmark-latest.json`，仓库保留本次提测的 `benchmark-results.json`。测试会在本地数据库创建独立集合。

技术设计与结果见 [一页技术说明](TECHNICAL.md)。
