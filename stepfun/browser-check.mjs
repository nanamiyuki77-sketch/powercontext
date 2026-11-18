import assert from 'node:assert/strict';
import { mkdir, writeFile } from 'node:fs/promises';
import { chromium } from 'playwright';

const browser = await chromium.launch({
  channel: process.env.BROWSER_CHANNEL || 'msedge',
  headless: process.env.HEADED !== '1',
});
try {
  const page = await browser.newPage();
  const base = 'http://127.0.0.1:5173';
  await page.goto(base + '/?wasm');
  const library = 'check_' + Date.now();
  await page.locator('#library').fill(library);
  await page.locator('#sample').click();
  await page.getByRole('status').filter({ hasText: '导入完成' }).waitFor({ timeout: 180_000 });
  assert.match(await page.locator('#backend').innerText(), /WASM/);
  await page.locator('#query').fill('WebGPU');
  await page.getByRole('button', { name: '检索' }).click();
  await page.locator('#results li').filter({ hasText: 'gpu.txt' }).first().waitFor();
  await page.locator('#files').setInputFiles({
    name: 'vermilion.txt', mimeType: 'text/plain', buffer: Buffer.from('vermilion orchid calibration procedure'),
  });
  await page.getByRole('button', { name: '导入文件' }).click();
  await page.getByRole('status').filter({ hasText: '导入完成' }).waitFor();
  await page.locator('#query').fill('vermilion orchid calibration');
  await page.getByRole('button', { name: '检索' }).click();
  await page.locator('#results li').filter({ hasText: 'vermilion.txt' }).waitFor();
  await page.locator('#library').fill(library + '_empty');
  await page.getByRole('button', { name: '检索' }).click();
  await page.getByRole('status').filter({ hasText: '没有匹配结果' }).waitFor();
  await page.reload();
  await page.locator('#library').fill(library);
  await page.locator('#query').fill('vermilion orchid calibration');
  await page.getByRole('button', { name: '检索' }).click();
  await page.locator('#results li').filter({ hasText: 'vermilion.txt' }).waitFor({ timeout: 180_000 });

  // Remove WebGPU before application code runs and verify automatic fallback.
  const fallback = await browser.newPage();
  await fallback.addInitScript(() => Object.defineProperty(navigator, 'gpu', { value: undefined }));
  await fallback.goto(base);
  await fallback.locator('#library').fill(library);
  await fallback.locator('#query').fill('WebGPU');
  await fallback.getByRole('button', { name: '检索' }).click();
  await fallback.locator('#results li').filter({ hasText: 'gpu.txt' }).first().waitFor({ timeout: 180_000 });
  assert.match(await fallback.locator('#backend').innerText(), /WASM.*未启用 WebGPU/);
  await fallback.close();
  console.log('UI checks passed: sample, incremental import, collection isolation, persistence, auto fallback');

  await page.goto(base + '/bench.html');
  await page.locator('#run').click();
  await page.waitForFunction(() => !document.querySelector('#run').disabled, null, { timeout: 600_000 });
  const report = JSON.parse(await page.locator('#report').innerText());
  report.browserVersion = browser.version();
  await mkdir('.data', { recursive: true });
  await writeFile(process.env.BENCH_OUTPUT || '.data/benchmark-latest.json', JSON.stringify(report, null, 2) + String.fromCharCode(10));
  console.table(report.report.map(({ repeats, ...summary }) => summary));
} finally {
  await browser.close();
}
