const corpus = [
  ['backups', 'Create regular database backups. To preserve data after a disk failure, save an encrypted copy on another device.'],
  ['gpu', 'WebGPU uses the graphics processor to accelerate inference in a web browser.'],
  ['wasm', 'WebAssembly runs the model on the CPU when the browser cannot initialize a graphics adapter.'],
  ['bicycle', 'Before riding a bicycle in rain, inspect both brakes and use front and rear lights.'],
  ['coffee', 'To make cold brew coffee, steep coarse grounds in cold water overnight and filter the liquid.'],
  ['index', 'A full-text index uses an inverted index to find documents that contain query terms.'],
  ['vectors', 'Vector similarity measures semantic relatedness even when documents do not use the same words.'],
  ['session', 'Choose a different collection to switch knowledge bases and isolate results between sessions.'],
  ['plants', 'Water a succulent only after its soil dries and place it near a bright window.'],
  ['baking', 'Bake sourdough bread in a covered dutch oven for the first part of the bake.'],
  ['quantization', 'Eight-bit weight quantization can reduce model download size and may affect accuracy.'],
  ['rail', 'The train timetable lists platform numbers and departure times for each service.'],
];
export const docs = corpus.map(([id, text]) => ({ id, source: id + '.txt', text }));
export const queries = [
  ['How do I preserve my data?', 'backups'],
  ['WebGPU', 'gpu'],
  ['What if my browser has no GPU?', 'wasm'],
  ['How to brew coffee without heating water?', 'coffee'],
  ['full-text index', 'index'],
  ['Make an embedding model smaller', 'quantization'],
  ['How do I separate documents into different libraries?', 'session'],
  ['What care does a succulent need?', 'plants'],
];
