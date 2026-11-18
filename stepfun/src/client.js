export async function post(action, body) {
  const response = await fetch('/api/' + action, {
    method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error);
  return result;
}

export async function ingest(name, docs, engine, batchSize = 8, progress) {
  let embeddingMs = 0;
  let dbMs = 0;
  let refreshMs = 0;
  let writeMs = 0;
  for (let i = 0; i < docs.length; i += batchSize) {
    const batch = docs.slice(i, i + batchSize);
    const start = performance.now();
    const vectors = await engine.encode(batch.map(d => d.text));
    embeddingMs += performance.now() - start;
    const writeStart = performance.now();
    const result = await post('import', {
      name, finish: i + batch.length === docs.length,
      rows: batch.map((d, j) => ({ ...d, vector: vectors[j] })),
    });
    writeMs += performance.now() - writeStart;
    dbMs += result.dbMs;
    refreshMs += result.refreshMs;
    progress?.(i + batch.length);
  }
  return { embeddingMs, dbMs, refreshMs, writeMs };
}

export async function search(name, text, engine) {
  const start = performance.now();
  const [vector] = await engine.encode([text]);
  const embeddingMs = performance.now() - start;
  const result = await post('search', { name, text, vector });
  return { ...result, embeddingMs, elapsedMs: performance.now() - start };
}
