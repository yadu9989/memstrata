// esbuild.config.js — MV3 bundle config for Memory Layer browser extension.
//
// Content scripts use IIFE format (safe for page injection, no module scope leaks).
// Service worker uses IIFE format (works without "type":"module" in manifest).
// Each entry point is fully self-contained; shared modules are inlined per bundle.

const esbuild = require('esbuild');
const fs = require('fs');
const path = require('path');

const isWatch = process.argv.includes('--watch');
const outdir = 'dist';

// Shared esbuild options
const commonOptions = {
  bundle: true,
  format: 'iife',
  target: 'chrome110',
  sourcemap: false,
  treeShaking: true,
  logLevel: 'info',
};

// Entry points grouped by build role
const contentScripts = [
  'src/content/universal_content_script.ts',
];

const uiScripts = [
  'src/popup/popup.ts',
  'src/options/options.ts',
];

const workerScript = [
  'src/service_worker.ts',
];

async function buildAll() {
  await esbuild.build({
    ...commonOptions,
    entryPoints: [...contentScripts, ...uiScripts, ...workerScript],
    outdir,
    outbase: 'src',
  });
  copyStaticAssets();
}

async function watchAll() {
  const ctx = await esbuild.context({
    ...commonOptions,
    entryPoints: [...contentScripts, ...uiScripts, ...workerScript],
    outdir,
    outbase: 'src',
  });
  await ctx.watch();
  copyStaticAssets();
  console.log('Watching for changes…');
}

function copyDirSync(src, dest) {
  if (!fs.existsSync(src)) return;
  fs.mkdirSync(dest, { recursive: true });
  for (const entry of fs.readdirSync(src, { withFileTypes: true })) {
    const srcPath  = path.join(src,  entry.name);
    const destPath = path.join(dest, entry.name);
    if (entry.isDirectory()) copyDirSync(srcPath, destPath);
    else fs.copyFileSync(srcPath, destPath);
  }
}

function copyStaticAssets() {
  const pairs = [
    ['manifest.json',             path.join(outdir, 'manifest.json')],
    ['src/popup/popup.html',      path.join(outdir, 'popup', 'popup.html')],
    ['src/options/options.html',  path.join(outdir, 'options', 'options.html')],
  ];

  for (const [src, dest] of pairs) {
    const dir = path.dirname(dest);
    fs.mkdirSync(dir, { recursive: true });
    if (fs.existsSync(src)) fs.copyFileSync(src, dest);
  }

  // public/ (icons, styles) — recursive copy preserves any nested structure
  copyDirSync('public', path.join(outdir, 'public'));
}

const run = isWatch ? watchAll : buildAll;
run().catch(err => { console.error(err); process.exit(1); });
