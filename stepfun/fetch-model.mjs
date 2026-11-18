import { spawnSync } from 'node:child_process';
import { mkdirSync, renameSync, statSync } from 'node:fs';
import { model, revision, modelFiles } from './src/model-info.mjs';

const root = 'public/models/' + model;
mkdirSync(root + '/onnx', { recursive: true });
for (const file of ['config.json', 'tokenizer.json', 'tokenizer_config.json',
  ...Object.values(modelFiles).map(name => 'onnx/' + name)]) {
  const path = root + '/' + file;
  const start = performance.now();
  if (!statSync(path, { throwIfNoEntry: false })) {
    const result = spawnSync('curl', ['--fail', '--location', '--retry', '2', '--connect-timeout', '20', '--max-time', '600', '--silent', '--show-error',
      'https://huggingface.co/' + model + '/resolve/' + revision + '/' + file + '?download=true',
      '--output', path + '.part'], { stdio: 'inherit' });
    if (result.status !== 0) throw new Error('下载失败：' + file);
    renameSync(path + '.part', path);
    console.log(file, statSync(path).size, 'bytes', Math.round(performance.now() - start), 'ms');
  } else console.log(file, statSync(path).size, 'bytes (cached)');
}
