// Genesis <-> weave website engine bridge.
//
// Runs weave's real website generator (src/portfolio/designInspirationResearcher.js and
// bespokeHtmlGenerator.js) from an unmodified weave checkout, in a child process started by
// genesis/web/builder.py. Reads one JSON request on stdin, writes one JSON result on stdout.
//
// Guard rails (weave's own fallbacks would otherwise hide failures or switch providers):
// - `dotenv` resolves to a no-op stub, so weave's .env is never read.
// - Only the Gemini API-key path may run: the agy CLI and Google OAuth / Cloud Code fallbacks are blocked.
// - Every Gemini call is recorded. If the page itself was not written by the model (weave would return a
//   canned template or the unchanged page), the result is an error, never a success.
// - Nothing is deployed: weave's orchestrator and Netlify deployer are not imported.

import fs from 'node:fs';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import * as nodeModule from 'node:module';

const STUB_DOTENV = 'data:text/javascript,' + encodeURIComponent(
  'export function config() { return { parsed: {} }; } export default { config };');

function blockDotenv() {
  if (typeof nodeModule.registerHooks === 'function') {
    nodeModule.registerHooks({
      resolve(specifier, context, next) {
        if (specifier === 'dotenv') return { url: STUB_DOTENV, shortCircuit: true };
        return next(specifier, context);
      },
    });
    return;
  }
  const hooks = `export async function resolve(s, c, n) {
    if (s === 'dotenv') return { url: ${JSON.stringify(STUB_DOTENV)}, shortCircuit: true };
    return n(s, c);
  }`;
  nodeModule.register('data:text/javascript,' + encodeURIComponent(hooks));
}

// weave logs progress with console.log; keep stdout for the JSON result only.
const log = (...a) => process.stderr.write(a.map(String).join(' ') + '\n');
console.log = log;
console.info = log;
console.warn = log;

function sanitize(text) {
  return String(text || '')
    .replace(/AIza[0-9A-Za-z_-]{20,}/g, '[redacted]')
    .replace(/(key=|x-goog-api-key["':\s]*)[^\s&"']+/gi, '$1[redacted]')
    .replace(/Bearer\s+[A-Za-z0-9._-]+/g, 'Bearer [redacted]')
    .slice(0, 600);
}

function readStdin() {
  return new Promise((resolve, reject) => {
    let data = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', (c) => { data += c; });
    process.stdin.on('end', () => resolve(data));
    process.stdin.on('error', reject);
  });
}

// Node's built-in fetch gives up when response headers take more than 300 s; Gemini can take longer than
// that to write a whole page (seen in practice: "fetch failed" at exactly 301 s). Gemini API requests go through
// node:https instead, which has no such limit. Genesis's own timeout (GENESIS_WEBSITE_TIMEOUT_SECONDS) still
// kills the whole bridge. Every other request keeps the normal fetch.
const GEMINI_HOST = 'generativelanguage.googleapis.com';
const nativeFetch = globalThis.fetch;
globalThis.fetch = async (input, init = {}) => {
  const url = new URL(typeof input === 'string' ? input : input.url);
  if (url.hostname !== GEMINI_HOST) return nativeFetch(input, init);
  const https = await import('node:https');
  return new Promise((resolve, reject) => {
    const req = https.request(url, { method: init.method || 'GET', headers: init.headers || {} }, (res) => {
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        const body = Buffer.concat(chunks).toString('utf8');
        resolve({ ok: res.statusCode >= 200 && res.statusCode < 300, status: res.statusCode,
          text: async () => body, json: async () => JSON.parse(body) });
      });
      res.on('error', reject);
    });
    req.on('error', reject);
    if (init.signal) init.signal.addEventListener('abort', () => req.destroy(new Error('aborted')));
    if (init.body) req.write(init.body);
    req.end();
  });
};

const calls = [];
let phase = 'setup';
const aiText = {}; // phase -> last raw model text

async function loadEngine(engineDir) {
  const src = path.join(engineDir, 'src');
  for (const f of ['gemini-client.js', 'portfolio/designInspirationResearcher.js', 'portfolio/bespokeHtmlGenerator.js']) {
    if (!fs.existsSync(path.join(src, f))) throw new Error(`website engine file missing: src/${f}`);
  }
  blockDotenv();
  const url = (f) => pathToFileURL(path.join(src, f)).href;
  const { geminiClient } = await import(url('gemini-client.js'));
  const research = await import(url('portfolio/designInspirationResearcher.js'));
  const bespoke = await import(url('portfolio/bespokeHtmlGenerator.js'));

  geminiClient.generateViaAgy = async () => { throw new Error('agy CLI is disabled by Genesis'); };
  geminiClient.resolveCloudCodeProject = async () => { throw new Error('Cloud Code OAuth is disabled by Genesis'); };
  const original = geminiClient.generateContent.bind(geminiClient);
  geminiClient.generateContent = async (prompt, system, config) => {
    if (!geminiClient.apiKey) {
      const err = new Error('GEMINI_API_KEY is not set for the website engine');
      calls.push({ phase, ok: false, error: err.message });
      throw err;
    }
    const started = Date.now();
    if (phase === 'research' && !config) {
      // weave parses this reply as JSON but doesn't ask for JSON; without JSON mode Gemini sometimes returns
      // invalid JSON and weave silently switches to canned directions.
      config = { responseMimeType: 'application/json' };
    }
    try {
      const res = await original(prompt, system, config);
      const usage = res?.raw?.usageMetadata || {};
      calls.push({ phase, ok: true, model: geminiClient.model, ms: Date.now() - started,
        input_tokens: usage.promptTokenCount ?? null, output_tokens: usage.candidatesTokenCount ?? null });
      aiText[phase] = res?.text || '';
      return res;
    } catch (e) {
      calls.push({ phase, ok: false, model: geminiClient.model, ms: Date.now() - started, error: sanitize(e.message) });
      throw e;
    }
  };
  return { research, bespoke };
}

function lastCall(p) {
  const list = calls.filter((c) => c.phase === p);
  return list[list.length - 1] || null;
}

function failure(p, fallbackMessage) {
  const c = lastCall(p);
  return c && !c.ok ? `${fallbackMessage}: ${c.error}` : fallbackMessage;
}

// True when `html` came out of the model's reply (weave only strips code fences), not a canned template.
function fromModel(p, html) {
  const text = aiText[p] || '';
  const c = lastCall(p);
  if (!c || !c.ok || !text || !html) return false;
  const probe = html.slice(0, 300).trim();
  return probe.length > 0 && text.includes(probe);
}

async function generate(req, engine) {
  const out = req.out_dir;
  phase = 'research';
  const research = await engine.research.researchDesignInspirations(req.prompt);
  // weave returns canned directions when the reply isn't valid JSON, even though the call itself succeeded.
  const firstName = String(research?.inspirations?.[0]?.name || '');
  const researchFromModel = Boolean(lastCall('research')?.ok && firstName && (aiText.research || '').includes(firstName));
  const topic = research.topicAnalysis || {};
  const inspirations = (research.inspirations || []).slice(0, 3);
  const count = Math.max(1, Math.min(3, Number(req.variants) || 1));
  const pages = [];
  for (let i = 0; i < count && i < inspirations.length; i++) {
    phase = `html-${i + 1}`;
    const html = await engine.bespoke.generateBespokeHtml({ prompt: req.prompt, topicAnalysis: topic, inspiration: inspirations[i] });
    if (!fromModel(phase, html)) {
      if (i === 0) return { ok: false, error: failure(phase, 'The AI did not produce the website page') };
      break; // extra design options are optional
    }
    const file = i === 0 ? 'index.html' : `option${i + 1}.html`;
    fs.writeFileSync(path.join(out, file), html, 'utf8');
    pages.push({ file, option: i + 1, name: String(inspirations[i]?.name || '').slice(0, 120),
      inspiration: String(inspirations[i]?.sourceInspiration || '').slice(0, 200) });
  }
  return {
    ok: true,
    brand_name: String(topic.brandName || '').slice(0, 60),
    headline: String(topic.headline || '').slice(0, 200),
    research: researchFromModel ? 'ai' : 'fallback',
    brand_from_research: researchFromModel,
    pages,
  };
}

async function modify(req, engine) {
  const current = fs.readFileSync(path.join(req.base_dir, 'index.html'), 'utf8');
  phase = 'redesign';
  const html = await engine.bespoke.redesignBespokeHtml({ currentHtml: current, feedbackText: req.feedback,
    brandName: req.brand_name || 'Website' });
  if (!fromModel(phase, html) || html === current) {
    return { ok: false, error: failure(phase, 'The AI did not apply the change') };
  }
  fs.writeFileSync(path.join(req.out_dir, 'index.html'), html, 'utf8');
  return { ok: true, brand_name: req.brand_name || '', pages: [{ file: 'index.html', option: 1, name: '', inspiration: '' }],
    truncated_input: current.length > 8000 };
}

async function main() {
  let result;
  try {
    const req = JSON.parse(await readStdin());
    if (!req.engine_dir || !req.out_dir) throw new Error('engine_dir and out_dir are required');
    const engine = await loadEngine(req.engine_dir);
    if (req.op === 'probe') result = { ok: true };
    else if (req.op === 'generate') result = await generate(req, engine);
    else if (req.op === 'modify') result = await modify(req, engine);
    else throw new Error(`unknown op: ${req.op}`);
  } catch (e) {
    result = { ok: false, error: sanitize(e?.message || e) };
  }
  result.calls = calls;
  process.stdout.write(JSON.stringify(result) + '\n');
}

main();
