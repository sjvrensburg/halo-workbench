# Translation bake-off — PLAN.md phase 6a

Run 2026-08-29 on the EVO-X3. **This file decides `TRANSLATE_ENDPOINT`; until it does,
phase 6b is not buildable.**

Harness: `scripts/bakeoff-translate.py`, cases in `tests/bakeoff/cases.json` — ten real
paragraphs, five `en→zh` and five `zh→en`, each with 2–5 protected spans (citation keys,
CLI flags, account numbers, dosages, brand names). Raw output, including every
translation, is in `tests/bakeoff/results-*.json`.

## What "mechanically clean" means here — and two corrections to the plan

PLAN.md 6b specifies the checks as "protected spans present **once and in order**, plus a
residual-script check". The first run of this bake-off falsified both halves of that.

**"Once" is wrong — it must be "as often as the source has it."** `--parallel` occurs
twice in the release-note case, and a faithful translation contains it twice. The check
is now count-in-output equals count-in-source.

**"In order" is wrong, and would fail good translations.** Chinese reorders. "Upgrade to
b10582 before enabling draft-mtp" comes back as
「在启用 draft-mtp 之前，请升级到 b10582」 — correct, idiomatic, and with the two protected
spans swapped. Under the original rule that is a failure; under any rule worth having it
is a pass. Order is still computed and reported, but it does not decide anything.

Left as specified: the residual-script check (Han left in English output, or Latin beyond
the protected spans in Chinese output) earns its place — it catches a model that echoed
the source instead of translating it, which happened.

## A methodology trap, recorded because it nearly produced the wrong answer

The first run scored **MiLMMT-46-1B at 0/10**, with empty replies on four cases and the
English source echoed verbatim on another. That number was meaningless: MiLMMT-46 is a
**completion** model whose chat template is a bare
`{% for message in messages %}{{ message.content }}{% endfor %}`, and it has no
instruction training at all. It had been sent an instruction-shaped chat prompt, so it
was being measured on a capability it does not claim.

Given its own documented template — `Translate this from {src} to {tgt}:` via
`/v1/completions`, with the trained language names ("Chinese (Simplified)", not
"Chinese") — and sentinel masking instead of instructions, the same model scored
**9/10**. Same model, same cases, same hardware, 0/10 → 9/10 on prompt shape alone.

Two consequences. First, every candidate here is run the way its own documentation says
to run it, and the arm is named in the results. Second, this is the reason the phase
exists: **a model without instruction support cannot be told what not to translate**, so
the mechanism differs per model, and the mechanism is part of what is being chosen.

## Results — the two candidates already on the box

| Candidate | How it was run | Mechanically clean | Wall clock | Per paragraph |
|---|---|---|---|---|
| **MiLMMT-46-1B** (`:8089`, Q8_0, ~1 GiB) | `/v1/completions`, native template, ZnZ sentinels | **9/10** | 3.6 s | **0.4 s** |
| **Ornith-1.5-35B-A3B** (`:8088`, the current provisional `TRANSLATE_ENDPOINT`) | chat, terminology instruction | **10/10** | 44.6 s | 4.5 s |
| Ornith-35B, same model | chat, ZnZ sentinels instead of instruction | 10/10 | 50.6 s | 5.1 s |

MiLMMT's one mechanical failure was the release-note case, where it emitted `--parallel`
three times against the source's two and leaked 11 Latin words past the protected spans.

### Run-to-run variance, at `temperature: 0`

Two identical runs of the Ornith arm, same prompt, same seed conditions, **produced
different text for 7 of the 10 paragraphs**. Both scored 10/10, but an earlier run of the
same arm lost all three protected spans on the legal clause. So:

* single-run bake-off scores carry real error bars, and this document's numbers should be
  read as one sample each, not as measurements to three significant figures;
* **span preservation by instruction is not reliable run to run.** That is the argument
  for 6b's checks running on *every request* rather than being validated once here — and
  it is an argument for sentinels plus a verifier over trusting any model's native lever,
  however well trained.

### Adequacy, judged by eye

Mechanical cleanliness and translation quality came apart, which is exactly the split
PLAN.md warned about ("a translation tool that quietly produces poor Chinese is worse than
no translation tool"). Reading the outputs:

**MiLMMT-46-1B has meaning errors in 3 of the 10**, all of the kind a user who cannot read
the target language would never catch:

| Case | Source | MiLMMT | Why it is wrong |
|---|---|---|---|
| econ-abstract | "falls by 12.4%" | 「下降了 12.4% **倍**」 | "12.4 percent **times**" — the unit contradicts the number |
| legal-clause | "the affected party **gives written notice** within 10 business days of becoming aware" | 「受影响方在 10 (ten) 工作日内**收到**相关事件的知晓通知」 | inverts who notifies whom, and drops "written" — a legally material change |
| technical-zh | 「surya 会误判为 NVIDIA 显卡」 | "surya will incorrectly **be interpreted as** a NVIDIA graphics card" | subject inverted: surya misjudges the GPU, it is not itself misjudged |

Smaller wobbles in the same set: 「特征」 (feature) for "specification", 「半参数性」 for
「半参数」, 「日常数据序列」 for a daily series, and "after six months" where the source says
"over six months".

**Ornith-35B was accurate in every case inspected**, including the three above, and reads
idiomatically in both directions. It is 11× slower per paragraph.

A test-design note for whoever extends the case set: `academic-zh` protects
「Linton 和 Mammen (2005)」, which contains the Chinese conjunction 和, so both models
correctly leave 和 sitting in an otherwise English sentence. The tool is behaving; the case
is badly chosen. Protected spans should not straddle a language boundary.

## Sentinels or the model's own instruction following?

Both mechanisms were run against Ornith-35B, the only candidate here that supports both:

| Mechanism | Clean | Per paragraph | Notes |
|---|---|---|---|
| terminology instruction ("X translates to X", keep it) | 10/10 | 4.5 s | reads naturally; the model sometimes translates the term anyway (an earlier run lost all three legal-clause spans) |
| ZnZ sentinels, restored after | 10/10 | 5.1 s | mechanical; restoration is exact by construction, not by the model's goodwill |

The sentinel form is not a free choice and was **not** re-derived here.
`../anti-watermark-mcp` measured seven forms over a Chinese round trip and found `[[0]]`
scores **0/21** on this very MiLMMT-1B — it collapses the doubled brackets and every span
is then lost — while `Z0Z` scores 21/21. Reused as-is.

The honest summary is that the two mechanisms are close on a model that follows
instructions, and only one of them exists on a model that does not. Since phase 6b runs
the verifier on every request regardless, sentinels are the safer default and the native
lever is an optimisation to be measured per model — the opposite of the hope that opened
this phase, and the reason the phase said "a native lever still has to be verified, not
trusted".

## Hy-MT2-7B (`tencent/Hy-MT2-7B-GGUF`, Q6_K, 5.8 GiB, Apache-2.0)

Downloaded and run on `:8094` with the same flags as the other units (`-ngl 999`,
`--flash-attn on`, q8_0 KV, `--load-mode dio`, `--parallel 1`, loopback only).

| Arm | Mechanically clean | Per paragraph |
|---|---|---|
| its own documented **terminology instruction** | **8/10** | 2.0 s |
| **ZnZ sentinels** | **10/10** | 1.5 s |

**The sentinels beat the native lever, on the model chosen for its native lever.** Under
instruction it lost `Dang and Ullah (2022)` from the econometrics abstract and `Agreement`
from the legal clause — translating them rather than keeping them. Under sentinels it lost
nothing, and was *faster*, because the sentinel form is shorter than the terminology
preamble.

That is the opposite of this phase's opening hypothesis ("a native lever beats a
hand-rolled one", from halo-prep pitfall #30). The lesson generalises differently than
expected: a native lever is still a model *choosing* to comply, and a sentinel is a string
substitution that either round-trips or is caught. Pitfall #30's case remains true — a
model's own effort flag beat a hand-spliced template — but there the lever was a
generation parameter, not an instruction the model must decide to follow.

Adequacy: **equal to Ornith-35B on every case inspected**, and correct on all three
paragraphs where MiLMMT introduced meaning errors — the notice direction in the legal
clause, the dosage timeline, and the surya subject inversion. Its econometrics register is
marginally weaker (「规格」 where 「规范」 is the term of art for "specification"); its
Chinese is otherwise idiomatic and its English is clean.

## Verdict

**Adopt Hy-MT2-7B on `:8094`, with ZnZ sentinels, not the native instruction.**

| | MiLMMT-46-1B | Ornith-35B | **Hy-MT2-7B** |
|---|---|---|---|
| best mechanical score | 9/10 | 10/10 | **10/10** |
| per paragraph | 0.4 s | 4.5 s | 1.5 s |
| adequacy | **3 meaning errors in 10** | clean | clean |
| already on disk | yes | yes (shared) | no — 5.8 GiB downloaded |
| contends with | `../anti-watermark-mcp` | **Open Notebook** and the podcast pipeline | nothing yet |

MiLMMT is rejected on adequacy, not on its score: it is 4× faster than the winner and
mechanically almost clean, and it still turns "falls by 12.4%" into "12.4 percent times"
and inverts who serves notice on whom. Those are precisely the errors a user who cannot
read the target language will never catch, which is the case this tool exists to serve.
It stays where it is, serving `../anti-watermark-mcp`, which round-trips through it for
paraphrase rather than presenting its output as a translation.

Ornith-35B is the honest fallback and costs nothing new. It is 3× slower per paragraph and
every request queues behind Open Notebook's, which is the argument for not using it.

### What adopting costs, all of it manual

* `config/llama-hymt2.service` in this repo, **on-demand, not enabled** — three units are
  already resident and the operator moved things off auto-start twice in August for GTT
  pressure. `:8094`, because `:8089` belongs to MiLMMT and `../anti-watermark-mcp` pins
  that unit by name through `AWM_TRANSLATOR_MODEL`.
* One hand-added line in halo-prep's `config/models.conf`, group `translation` so
  `model-switch.sh` never leaves two translators fighting for GTT:
  `hymt2-7b|llama-hymt2.service|8094|~6|translation`
* No `tailscale serve` registration: the workbench proxies it.
* `TRANSLATE_ENDPOINT` in `app/config.py` changes from the provisional `:8088` to `:8094`,
  and stops being provisional.

### What this changes in phase 6b

1. **Sentinels are the mechanism, and the native instruction is not a fallback but a
   discarded option.** Measured 8/10 against 10/10 on the same model, same paragraphs.
2. **The three checks stand, with the two corrections above** — count-in-source rather than
   "once", and order reported rather than required.
3. **The verifier is not optional and not a formality.** Two `temperature: 0` runs of the
   same arm differed on 7 of 10 paragraphs, and span loss appeared and disappeared between
   runs. Whatever wins a bake-off, the request in front of the user is a fresh sample.
4. A UX consequence, found by writing the cases: protecting a span means *do not
   translate it*, so protecting 「新华社」 in a zh→en job leaves 「新华社」 sitting in the
   English output — correct behaviour, surprising result. The span UI has to make that
   visible before the request, not after.
