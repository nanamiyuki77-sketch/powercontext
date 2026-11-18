import { loadModel } from './model.js';
import { modelFiles, model } from './model-info.mjs';
import { docs, queries } from './fixtures.js';
import { ingest, search } from './client.js';

const median = (sample) => [...sample].sort((a, b) => a - b)[Math.floor(sample.length / 2)];
async function size(dtype) {
  const prefix = '/models/' + model + '/';
  let bytes = 0;
  for (const file of ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'onnx/' + modelFiles[dtype]]) {
    const response = await fetch(prefix + file);
    if (!response.ok || response.headers.get('Content-Type')?.includes('text/html'))
      throw new Error('模型文件缺失：' + file + '。先运行 npm run models，再启动或构建应用。');
    bytes += (await response.blob()).size;
  }
  return bytes;
}

export async function benchmark() {
  const report = [];
  const sizes = new Map();
  const runID = 'bench_' + crypto.randomUUID().replaceAll('-', '').slice(0, 12);
  const configs = [
    ['fp32', true, 1], ['q8', true, 1], ['q8', true, 8], ['q8', false, 8],
  ];
  for (const [index, [dtype, forceWasm, batch]] of configs.entries()) {
    const engine = await loadModel({ dtype, forceWasm });
    try {
      if (!sizes.has(dtype)) sizes.set(dtype, await size(dtype));
      const bytes = sizes.get(dtype);
      const repeats = [];
      for (let i = 0; i < 3; i++) {
        const name = runID + '_' + index + '_' + i;
        const timings = await ingest(name, docs, engine, batch);
        const first = await search(name, queries[0][0], engine);
        if (!first.hits.some(hit => hit.id === queries[0][1]))
          throw new Error('写入后首查未找到预期文档');
        const samples = [];
        for (const [text, relevant] of queries) {
          const result = await search(name, text, engine);
          const rank = result.hits.findIndex(hit => hit.id === relevant) + 1;
          samples.push({ query: text, rank, embeddingMs: result.embeddingMs,
            dbMs: result.dbMs, elapsedMs: result.elapsedMs });
        }
        repeats.push({ ...timings, first, samples });
      }
      const samples = repeats.flatMap(r => r.samples);
      report.push({
        dtype, device: engine.device, fallback: engine.fallback, batch, bytes, loadMs: engine.loadMs,
        docPerSecond: median(repeats.map(r => docs.length * 1000 / r.embeddingMs)),
        dbWriteMs: median(repeats.map(r => r.dbMs)), writeMs: median(repeats.map(r => r.writeMs)),
        dbRefreshMs: median(repeats.map(r => r.refreshMs)),
        firstQueryMs: median(repeats.map(r => r.first.elapsedMs)),
        queryEmbeddingMs: median(samples.map(s => s.embeddingMs)),
        dbSearchMs: median(samples.map(s => s.dbMs)), queryMs: median(samples.map(s => s.elapsedMs)),
        recallAt5: samples.filter(s => s.rank > 0).length / samples.length,
        ndcgAt5: samples.reduce((sum, s) => sum + (s.rank ? 1 / Math.log2(s.rank + 1) : 0), 0) / samples.length,
        repeats,
      });
    } finally {
      await engine.dispose();
    }
  }
  return { userAgent: navigator.userAgent, documents: docs.length, queries: queries.length, report };
}

const output = document.querySelector('#report');
document.querySelector('#run').onclick = async event => {
  event.target.disabled = true;
  output.textContent = '运行中…';
  try {
    output.textContent = JSON.stringify(await benchmark(), null, 2);
  } catch (error) {
    output.textContent = error.stack || error.message;
  } finally {
    event.target.disabled = false;
  }
};
