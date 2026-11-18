import { env, pipeline } from '@huggingface/transformers';

import { model, revision } from './model-info.mjs';
env.allowLocalModels = true;
env.allowRemoteModels = false;
env.localModelPath = '/models/';
env.backends.onnx.wasm.numThreads = 1;

export async function loadModel({ dtype = 'q8', forceWasm = false, progress } = {}) {
  const start = performance.now();
  const init = async (device) => {
    const extractor = await pipeline('feature-extraction', model, { device, dtype, revision, progress_callback: progress });
    const encode = async (texts) => (await extractor(texts, { pooling: 'mean', normalize: true })).tolist();
    try {
      await encode(['warmup']);
    } catch (error) {
      await extractor.dispose();
      throw error;
    }
    return { device, encode, dispose: () => extractor.dispose(), loadMs: performance.now() - start };
  };
  let fallback;
  if (!forceWasm && navigator.gpu) {
    try {
      if (await navigator.gpu.requestAdapter()) return await init('webgpu');
      fallback = '没有可用的 WebGPU 适配器';
    } catch (error) {
      console.warn('WebGPU initialization failed', error);
      fallback = 'WebGPU 初始化或试运行失败';
    }
  } else if (!forceWasm) {
    fallback = '浏览器未启用 WebGPU';
  }
  return { ...await init('wasm'), fallback };
}
