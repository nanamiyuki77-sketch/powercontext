import './style.css';
import { loadModel } from './model.js';
import { ingest, search } from './client.js';
import { docs } from './fixtures.js';

const $ = id => document.getElementById(id);
let loading;
let busy = false;
const status = text => { $('status').textContent = text; };

function engine() {
  loading ??= loadModel({
    forceWasm: new URLSearchParams(location.search).has('wasm'),
    progress: event => {
      if (event.status === 'progress') status('加载 ' + event.file + '：' + Math.round(event.progress) + '%');
    },
  }).then(model => {
    $('backend').textContent = '推理后端：' + model.device.toUpperCase() + ' · INT8' +
      (model.fallback ? '（' + model.fallback + '）' : '');
    return model;
  }).catch(error => { loading = undefined; throw error; });
  return loading;
}

async function run(action) {
  if (busy || !$('library').reportValidity()) return;
  const name = $('library').value + '_q8';
  busy = true;
  const controls = [...document.querySelectorAll('button, input')];
  controls.forEach(control => { control.disabled = true; });
  $('results').replaceChildren();
  try {
    await action(name);
  } catch (error) {
    status(error.message);
  } finally {
    busy = false;
    controls.forEach(control => { control.disabled = false; });
  }
}

async function importDocs(name, items) {
  if (!items.length) throw new Error('没有可导入的文本');
  const model = await engine();
  let written = 0;
  try {
    await ingest(name, items, model, 8, count => {
      written = count;
      status('已写入 ' + count + ' / ' + items.length + ' 个片段');
    });
  } catch (error) {
    throw new Error('已写入 ' + written + ' 个片段。可重试导入。' + error.message);
  }
  status('导入完成，共 ' + written + ' 个片段。');
}

$('sample').onclick = () => run(name => importDocs(name, docs));
$('import').onclick = () => run(async name => {
  const items = [];
  for (const file of $('files').files) {
    if (file.size > 1024 * 1024) throw new Error(file.name + ' 超过 1 MiB');
    const input = new TextDecoder('utf-8', { fatal: true }).decode(await file.arrayBuffer());
    // shortcut: fixed character chunks for MiniLM; use tokenizer-aware splitting for longer or multilingual corpora.
    const chunks = input.match(/[\s\S]{1,600}/gu) || [];
    for (const [i, text] of chunks.entries()) {
      if (!text.trim()) continue;
      const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify([file.name, i, text])));
      const id = Array.from(new Uint8Array(hash), byte => byte.toString(16).padStart(2, '0')).join('');
      items.push({ id, source: file.name.slice(0, 255), text });
    }
  }
  await importDocs(name, items);
});

$('library').oninput = () => { $('results').replaceChildren(); status(''); };
$('search').onsubmit = event => {
  event.preventDefault();
  run(async name => {
    const text = $('query').value.trim();
    if (!text) throw new Error('请输入查询');
    const model = await engine();
    status('检索中…');
    const result = await search(name, text, model);
    for (const hit of result.hits) {
      const li = document.createElement('li');
      const source = document.createElement('strong');
      source.textContent = hit.source || hit.id;
      const content = document.createElement('p');
      content.textContent = hit.text || '';
      li.append(source, content);
      $('results').append(li);
    }
    status(result.hits.length ? '找到 ' + result.hits.length + ' 条结果 · ' +
      result.elapsedMs.toFixed(0) + ' ms' : '当前知识库没有匹配结果。');
  });
};
