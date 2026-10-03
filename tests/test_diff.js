// node tests/test_diff.js — the panel's word diff (extension/panel/diff.js)
"use strict";
const assert = require("assert");
const D = require("../extension/panel/diff.js");

const before = "La radio etudiante du college est a la recherche de nouveau collaborateurs.";
const after = "La radio étudiante du collège est à la recherche de nouveaux collaborateurs.";
const segs = D.diff(before, after);
const ch = D.changes(segs);
assert.deepStrictEqual(ch.map((c) => [c.old, c.new]),
  [["etudiante", "étudiante"], ["college", "collège"], ["a", "à"], ["nouveau", "nouveaux"]]);
assert.strictEqual(D.compose(segs, ch.map(() => true)), after);
assert.strictEqual(D.compose(segs, ch.map(() => false)), before);
assert.strictEqual(D.compose(segs, [true, false, true, false]),
  "La radio étudiante du college est à la recherche de nouveau collaborateurs.");
assert.ok(D.similarity(before, segs) > 0.6);

// a heavy rewrite reads as low similarity
const rewrite = D.diff(before, "Le collège cherche du monde pour sa radio.");
assert.ok(D.similarity(before, rewrite) < 0.35, D.similarity(before, rewrite));
assert.strictEqual(D.compose(rewrite, D.changes(rewrite).map(() => true)), "Le collège cherche du monde pour sa radio.");

// an added comma and a two-word replacement stay one change each
const s2 = D.diff("le mardi midi devant public traitant de", "le mardi midi, devant public qui traite de");
assert.deepStrictEqual(D.changes(s2).map((c) => [c.old, c.new]), [["", ","], ["traitant", "qui traite"]]);

// identical text: no changes; paragraphs (newlines) survive composing
assert.strictEqual(D.changes(D.diff("Même texte.", "Même texte.")).length, 0);
const p = D.diff("Un.\nDeux trois.", "Un.\nDeux, trois.");
assert.strictEqual(D.compose(p, [true]), "Un.\nDeux, trois.");

// very long text falls back to one whole change instead of a huge table
const big = "mot ".repeat(3000);
const fb = D.diff(big, big + "fin");
assert.strictEqual(D.changes(fb).length, 1);
assert.strictEqual(D.compose(fb, [true]), big + "fin");
console.log("diff: all good");
