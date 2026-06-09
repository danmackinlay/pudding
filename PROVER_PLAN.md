# PROVER_PLAN — Track 2: the prover + assisted formalisation (handoff)

Self-contained handoff for a fresh instance working **in the `../pudding-prover` worktree**
(`claude/prover`; see `FORK.md`). This is the gated, higher-risk frontier. Read, in order:
`PLAN.md` §5 (the prover spike + the two gates), `PROVER_RESEARCH_ADDENDUM.md` (the verified Lean
infra — do **not** re-derive it), `FINDINGS.md` (trust ladder), `STUDIO_PLAN.md` (the `Job`/widget
the prover reuses — decision #7), and `FORK.md` (the frozen-core contract).

## 0. The two gates (PLAN §5 — both must hold before building much)
1. **Name a problem you actually want *bulletproof*.** Without a real target the prover is a toy.
2. **A usability answer for Lean proof state.** No chat renders it; the **studio** can (embed
   `lean4web`, or just render the compiler verdict + proof text + the faithfulness dossier).

## 1. The core insight (why this track is pudding-shaped)
**The bottleneck is *stating* the theorem, not proving it.** Proof search is now cheap: a prebuilt
Lean verifier image, metered DeepSeek-Prover-V2, generalists that close `sorry`s, and Pass@k against
an **unfakeable compiler**. The hard part is **autoformalisation (~55–75% faithful) — and its failures
are silent**: a plausible Lean `theorem` that means something subtly different (or is vacuously /
trivially true), which Lean then stamps ✓, **laundering a wrong statement into false confidence.**
That is strictly worse than no proof, and it's the `maj@8 says 43` pathology (FINDINGS) with the
verifier's authority on top. **A verified proof is only as trustworthy as the faithfulness of its
statement — and faithfulness is exactly what the verifier cannot check.**

So this track applies pudding's own discipline one level up: **AI proposes formalisations abundantly,
cheap oracles dispose of the unfaithful ones, the human curates the survivors, the prover proves.**
The product isn't "we autoformalise" (everyone's mediocre at that) — it's *"we make formalisation
**auditable** and refuse to launder a green checkmark."*

## 2. What exists to reuse (do not rebuild)
- **Verified Lean infra (PROVER_RESEARCH_ADDENDUM.md):** `modal.Image.from_registry(
  "projectnumina/kimina-lean-server:2.0.0")` (Mathlib v4.26.0 baked in), run in a `modal.Sandbox`
  with the **restart-on-timeout** pattern; verify via `POST /api/check` with
  `{"snippets":[{"id","code"}],"timeout":…}` (a closed proof = no error-severity message). The
  addendum has the exact request/response parsing and the Sandbox lifecycle — copy it, don't guess.
- **Prover model routes:** DeepSeek-Prover-V2-671B metered on **Novita** (OpenAI-compatible,
  `https://api.novita.ai/openai`, needs a Novita key) *or* a generalist (Kimi/Opus) with a
  "close this `sorry`" prompt. Both are just a `model`+`provider` in the existing registry.
- **The parked skeleton:** `prover_loop.py` (the TIR-shaped loop with a Lean verifier in place of the
  Python kernel; stop when the compiler accepts), `verifier/` (e.g. `modal_verifier.py`, which already
  references `prover_loop`), `fanout.py` (the Modal `.map` Pass@k template — **this** is the right
  fan-out for the prover: a sandbox-per-call, not the generalist asyncio path; FORK/PLAN §4).
- **The falsify oracle (`pudding/discovery.py`):** the isolated-subprocess sympy/numpy runner. Reuse
  it for **numeric faithfulness checks** (below). If you need to extend it ("compare a formal
  statement's concrete instances against the informal harness"), that's a **core change → land on
  base first** (FORK.md rule 2).
- **The Job/widget surface (STUDIO_PLAN decision #7):** the prover is a **reducer swap** — engine =
  the Lean verifier, "vote" = first/ranked candidate that compiles (Pass@k, cheap because the
  compiler is unfakeable). The answer-cluster board becomes a **proof gallery**; the conjecture flock
  becomes **formalisation candidates with faithfulness badges**. Same `Job`, same envelope, same store.

## 3. The trust ladder, extended to formalisation
`maj@k → cross-model → self-verify → numeric oracle → [FAITHFULNESS GATE] → Lean ✓`
The Lean rung's trust is **conditional** on the gate. The honest render is always:
*"proof verified · formalisation faithfulness: [type-checks ✓ · numeric checks N/N · back-translation
3/3 agree · negation not provable]"* — surfaced, never hidden.

Named failure modes the gate must catch: wrong quantifier/hypothesis → vacuously true; ℕ truncated
subtraction or `/` as integer division silently changing meaning; `Nat.Prime` vs the informal
"prime"; trivial restatement (`2 = 2 := rfl`). All compile; some prove; none mean what was intended.

## 4. Build order (gates first, then the USP)

- **Q1 — formal-first proof spike (no autoformalisation; cheapest honest step; PLAN §5's "one
  hand-written MiniF2F statement, feel the friction").** Stand up the Kimina verifier on Modal
  (addendum recipe), add a `prove` strategy = Pass@k: fan out k proof attempts (Novita prover or a
  generalist closing `sorry`), each checked by the verifier; reducer = first/ranked that compiles.
  Human supplies the Lean `theorem … := by sorry`. **DoD:** paste a formal statement, get a
  verified proof (or an honest "no candidate compiled in k"), driven through the existing `Job`.
- **Q2 — the proof-gallery widget (studio).** Reducer swap on the identical `Job`/widget: ranked
  **verified** proofs (length · time · cost), the compiler verdict, proof text (and `lean4web` embed
  if the usability gate wants it). **DoD:** a verified-proof gallery for a statement, in the studio,
  reusing the board's stream-fill/kill/persist machinery.
- **Q3 — assisted formalisation with the faithfulness gate (the USP).** A `formalize` verb shaped
  like `conjecture`: generate **k** Lean formalisations of an informal statement, then the cheap
  oracles **dispose** before any proof search —
  (a) *elaboration* — does it type-check? (the Kimina server, no proof needed);
  (b) *non-vacuity* — provable by `trivial`/`rfl`/`decide`? is the negation also provable? flag it;
  (c) **numeric faithfulness** — reuse the falsify oracle: the informal statement already ships (or
  can be given) a `counterexample()`-style harness; check the **Lean statement's concrete instances**
  (via `#eval`/`decide` for the decidable cases) **agree** with that harness on n = 2,3,5,…; a
  disagreement = unfaithful, disposed of for ~free, exactly like a false conjecture;
  (d) **back-translation consensus** — informalise the Lean back to English with k models, compare to
  the original (informalisation is more reliable than formalisation; round-trip disagreement =
  unfaithfulness signal).
  Then the **human curates** the survivors via a **faithfulness dossier**, signs off, and only then
  does Q1 prove. **DoD:** from an informal statement, produce faithful Lean-statement candidates each
  with an auditable dossier; human picks one; it gets proved; the render is the honest conditional
  string in §3.
- **Q4 — (stretch) close the discovery→prove loop.** Wire `discovery`'s falsify-survivors → Q3
  formalisation → Q1 proof: the full `generate → falsify → formalise → prove` pipeline, each stage a
  cheap-filter-before-expensive-step. **DoD:** a survived conjecture flows end-to-end to a verified
  (and faithfulness-audited) Lean proof.

## 4½. Glass-box reframe — draft-sketch-prove, human-curated (decided 2026-06-09)

Context: by mid-2026 the SOTA moved from "one fine-tuned model emits a whole proof" to **hybrid
harnesses** — iterative refinement (the single largest driver) + recursive decomposition (the
frontier) — wrapping a **generalist reasoner** that matters more than the Lean specialist;
autoformalisation has moved *inside* the loop ("draft-sketch-prove is the spine"). See
`livingthing/notebook/ai_reasoning.qmd#prover-architectures`. Q1 already sits on the right side of
this (rents a generalist + does iterative refinement); the only gap was decomposition.

What changes — and doesn't: the **USP is unchanged** (auditable faithfulness, refuse to launder a
green check; §7's "don't compete on Pass@k" still holds). The objective is a **glass-box** harness —
friendly · interactive · transparent · human-steerable — the opposite of the closed, $/problem
leaders (Aleph, Seed-Prover, Aristotle), which we explicitly do **not** chase.

Key insight: **decomposition is a *transparency* feature, not a Pass-rate trick.** A whole-proof
Pass@k loop is a black box about its process; a draft-sketch-prove tree is human-readable (the
informal sketch *is* the plan) and the compiler validates the *skeleton's logic* before any hole is
proved. So build the transparent core, skip the walled-garden machinery:

  BUILD: informal sketch w/ `sorry` holes · per-lemma faithfulness gate + dossier · human curation
         · **Q1 as the leaf-prover** (close one atomic goal — done) · thin recursion.
  SKIP : Mathlib semantic retriever · MCTS / self-retraining (Pass-rate machinery, opaque).

Effect on the Q-order: Q1 (done) is the leaf-prover. **Next = the draft-sketch spike** (below).
Q2's studio becomes the **sketch-tree + per-lemma dossier curation board** (not just a gallery).
Q3's faithfulness gate audits **sub-lemmas**, not only the top statement. Q4 (discovery→prove) gains
the sketch step in the middle.

**Draft-sketch spike (the next build)** — new fork module `pudding/sketch.py`:
  1. Generalist drafts a **flat decomposition**: standalone `lemma hᵢ : Tᵢ := by sorry` leaves + a
     top `theorem target := by <combine the hᵢ>`.
  2. **Skeleton validity** — Kimina elaborates the lemmas-as-sorry + the combine step: an *error* ⇒
     the sketch's logic is broken (revise the sketch); *only sorries* ⇒ the decomposition is valid.
     The glass-box win: the compiler checks the plan before we spend on leaves.
  3. Close each leaf independently with `prove_one_async` (Q1, Pass@k per leaf — parallel).
  4. Reassemble (splice the found proofs) and **final-verify the whole file** (the real verdict).
  5. Render the tree: skeleton + per-hole status (✓ closed / ✗ open / sketch-invalid).
  **DoD:** a multi-step statement decomposes, the skeleton type-checks, the leaves are closed by Q1,
  the reassembled proof compiles end-to-end, and the tree is human-readable. Reuses the Kimina
  verifier + `prove_one_async`; no shared-core change.

## 5. Architecture rules (inherited from FORK.md)
- The prover is a **reducer + engine swap** on the existing `Job`/envelope/widget — not a new job
  layer. If you're rebuilding orchestration, you're diverging the core.
- **Modal is for the prover**, because a per-call owns a Lean sandbox (sandbox-per-call Pass@k via
  `fanout.py`) — *not* the rented-generalist asyncio path (PLAN §4).
- Any change to the shared `pudding` library / streaming envelope / store / oracle → **base first**,
  then merge forward. New prover code lives in new modules (`pudding/prove.py`, `pudding/formalize.py`)
  + the parked `prover_loop.py` / `verifier/` / `fanout.py`.

## 6. Open questions
- **Faithfulness dossier UX:** how much to automate vs. require human sign-off (default: never
  auto-accept a formalisation; the human gate is a feature, not a failure).
- **Numeric spot-check extraction:** evaluating a Lean statement's instances (`#eval`/`decide` over a
  `Decidable` instance) vs. running the informal harness and comparing — start with decidable finite
  cases; non-decidable statements get only elaboration + back-translation.
- **Cost/latency of Pass@k** against a metered prover + a Modal sandbox: budget + auto-stop-on-first-verified.
- **Gate (1):** which bulletproof-worthy target anchors the spike (MiniF2F? a lemma from the solver's
  surviving conjectures? a result Dan actually needs)?

## 7. Non-goals
Production multi-user proving; a Lean tutor; competing with dedicated provers on raw Pass@k — the
differentiator is the **auditable faithfulness gate**, not proof-search SOTA. Don't touch the solver
UX (Track 1). Don't treat autoformalisation as a solved black box.

## 8. Starting state
Branch off the base `6193fbf` (audit taut-up; 66 tests green). The library, oracle, and Job/widget
are stable and contract-frozen. Confirm both §0 gates with Dan, then start with **Q1** (it de-risks
the verifier infra independent of the formalisation problem).
