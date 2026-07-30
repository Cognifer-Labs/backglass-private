#!/usr/bin/env node
/**
 * Validates the design system palette against both surfaces.
 * Run after ANY color change:  node scripts/validate-palette.mjs
 * Exits non-zero on failure so it can gate CI.
 *
 * Checks:
 *   1. every ink clears 3:1 on both paper and black (gold is a documented exception)
 *   2. the three chart series pass all-pairs CVD separation in both modes
 *   3. text tokens clear WCAG AA on their intended surface
 */
import { validate, contrast } from './_validator_core.js';

const PAPER = '#FAF3DF';
const BLACK = '#000000';

const INKS = {
  vermilion: '#D03D37',
  gold:      '#E8AC1D',
  green:     '#249041',
  turquoise: '#009592',
  cobalt:    '#2766C0',
};

// Documented exceptions. Adding to this list is how a palette stops being accessible;
// each entry needs a written justification in design/design-system.md.
const EXCEPTIONS = {
  gold: 'sub-3:1 on paper by design; black keyline plus text label is the mitigation',
};

const SERIES = ['#2766C0', '#D03D37', '#009592'];

const TEXT = [
  ['#000000', PAPER, 4.5, 'black ink on paper'],
  ['#3d3c37', PAPER, 4.5, 'neutral 700 secondary on paper'],
  ['#716f67', PAPER, 4.5, 'neutral 500 muted on paper'],
  [PAPER, BLACK, 4.5, 'paper text on black'],
  ['#c9c4b5', BLACK, 4.5, 'neutral 200 secondary on black'],
  ['#aca79b', BLACK, 4.5, 'neutral 300 muted on black'],
];

let failed = false;
const pass = (ok) => (ok ? 'PASS' : (failed = true, 'FAIL'));

console.log(`\nSurfaces: paper ${PAPER} (${contrast(PAPER, BLACK).toFixed(2)}:1 vs black)\n`);

console.log('INKS — 3:1 on both surfaces');
for (const [name, hex] of Object.entries(INKS)) {
  const p = contrast(hex, PAPER), b = contrast(hex, BLACK);
  const ok = (p >= 3 && b >= 3) || name in EXCEPTIONS;
  const note = name in EXCEPTIONS ? `  (exception: ${EXCEPTIONS[name]})` : '';
  console.log(`  ${pass(ok)}  ${name.padEnd(10)} ${hex}  paper ${p.toFixed(2)}  black ${b.toFixed(2)}${note}`);
}

console.log('\nSERIES — all-pairs CVD, both modes');
for (const [mode, surface] of [['light', PAPER], ['dark', BLACK]]) {
  const r = validate(SERIES, { mode, surface, pairs: 'all' });
  console.log(`  ${pass(r.ok)}  ${mode}`);
  for (const [check, verdict, detail] of r.report) {
    console.log(`         ${check.padEnd(22)} ${String(verdict).padEnd(6)} ${detail}`);
  }
}

console.log('\nTEXT — WCAG AA');
for (const [fg, bg, min, label] of TEXT) {
  const c = contrast(fg, bg);
  console.log(`  ${pass(c >= min)}  ${c.toFixed(2).padStart(6)}  ${label}`);
}

console.log(`\n${failed ? 'FAILED — fix before shipping' : 'All checks passed'}\n`);
process.exit(failed ? 1 : 0);
