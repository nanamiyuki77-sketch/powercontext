import { defineConfig } from 'vite';
import { SeekdbClient, Schema, FulltextIndexConfig, VectorIndexConfig } from 'seekdb';
import { resolve } from 'node:path';

const client = new SeekdbClient(process.env.SEEKDB_HOST ? {
  host: process.env.SEEKDB_HOST, port: Number(process.env.SEEKDB_PORT || 2881),
  user: process.env.SEEKDB_USER || 'root', password: process.env.SEEKDB_PASSWORD || '',
  database: process.env.SEEKDB_DATABASE || 'test',
} : { path: resolve('.data/seekdb'), database: 'test' });

const schema = new Schema({
  fulltextIndex: new FulltextIndexConfig(),
  vectorIndex: new VectorIndexConfig({
    hnsw: { dimension: 384, distance: 'cosine' }, embeddingFunction: null,
  }),
});
const collections = new Map();
function collection(name) {
  if (!collections.has(name)) {
    const opening = client.getOrCreateCollection({ name, schema, embeddingFunction: null });
    collections.set(name, opening);
    opening.catch(() => collections.delete(name));
  }
  return collections.get(name);
}

const validVector = (v) => Array.isArray(v) && v.length === 384 && v.every(Number.isFinite);
async function jsonBody(req) {
  const buffers = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 512 * 1024) throw new Error('请求超过 512 KiB');
    buffers.push(chunk);
  }
  return JSON.parse(Buffer.concat(buffers).toString('utf8'));
}

let pending = Promise.resolve();

function install(server) {
  server.middlewares.use(async (req, res, next) => {
    if (req.url !== '/api/import' && req.url !== '/api/search') return next();
    const reply = (status, value) => {
      res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8' });
      res.end(JSON.stringify(value));
    };
    if (req.method !== 'POST' || !/^application\/json(?:;|$)/i.test(req.headers['content-type'] || '')
      || (req.headers.origin && req.headers.origin !== 'http://' + req.headers.host)) {
      return reply(403, { error: '只接受同源 JSON POST' });
    }
    let body;
    try {
      body = await jsonBody(req);
      if (!body || typeof body.name !== 'string' || !/^[a-z][a-z0-9_]{2,58}$/.test(body.name))
        throw new Error('知识库名称无效');
      if (req.url === '/api/import') {
        if (typeof body.finish !== 'boolean' || !Array.isArray(body.rows) || body.rows.length < 1 || body.rows.length > 32 ||
          body.rows.some(r => !r || typeof r.id !== 'string' || r.id.length > 100 ||
            typeof r.source !== 'string' || r.source.length > 255 ||
            typeof r.text !== 'string' || !r.text.trim() || r.text.length > 1400 || !validVector(r.vector)))
          throw new Error('文本或向量批次无效');
      } else if (typeof body.text !== 'string' || !body.text.trim() || body.text.length > 256 ||
        !validVector(body.vector)) throw new Error('查询或向量无效');
    } catch (error) {
      return reply(400, { error: error.message });
    }
    try {
      const previous = pending;
      let release;
      pending = new Promise(resolve => { release = resolve; });
      await previous;
      try {
        const db = await collection(body.name);
        const start = performance.now();
        if (req.url === '/api/import') {
          await db.upsert({
            ids: body.rows.map(r => r.id), documents: body.rows.map(r => r.text),
            metadatas: body.rows.map(r => ({ source: r.source })), embeddings: body.rows.map(r => r.vector),
          });
          const writeDbMs = performance.now() - start;
          const refreshStart = performance.now();
          if (body.finish) await db.refresh_index();
          return reply(200, { dbMs: writeDbMs, refreshMs: body.finish ? performance.now() - refreshStart : 0 });
        }
        const keywords = body.text.match(/[\p{L}\p{N}]+/gu)?.join(' ') || '';
        const result = await db.hybridSearch({
          query: keywords ? { whereDocument: { $contains: keywords } } : undefined,
          knn: { queryEmbeddings: [body.vector] }, rank: { rrf: {} }, nResults: 5,
          include: ['documents', 'metadatas'],
        });
        const dbMs = performance.now() - start;
        const hits = (result.ids?.[0] || []).map((id, i) => ({
          id, text: result.documents?.[0]?.[i], source: result.metadatas?.[0]?.[i]?.source,
        }));
        reply(200, { hits, dbMs });
      } finally {
        release();
      }
    } catch (error) {
      console.error(error);
      reply(500, { error: 'seekdb 操作失败。请检查启动终端。' });
    }
  });
}

export default defineConfig({
  build: { rollupOptions: { input: { app: resolve('index.html'), bench: resolve('bench.html') } } },
  plugins: [{ name: 'local-seekdb', configureServer: install, configurePreviewServer: install }],
  server: {
    host: '127.0.0.1', port: 5173, strictPort: true,
    fs: { deny: ['.env', '.env.*', '*.{crt,pem}', '**/.git/**', '**/.data/**'] },
  },
  preview: { host: '127.0.0.1', port: 5173, strictPort: true },
});
