# Dataset licenses & attribution

The A/B benchmark (`run_ab.py`) runs items pulled **verbatim** from recognized-standard
public datasets. We invent no question content. This file records each source, its
pinned revision, and its license. The **code** in this repo is Apache-2.0; the **data**
below is distributed under its own license, which coexists with (and is not overridden
by) the code license.

`build_public_dataset.py --hf` reads each corpus at the pinned revision. The checked-in
`public_dataset.jsonl` may be a **structural placeholder** built from the offline fixture
(`build_source: "fixture"` in `public_dataset.meta.json`) — regenerate it from Hugging
Face before publishing any headline number.

| Profile | Dataset | Hugging Face id | Revision | License | Grading |
|---------|---------|-----------------|----------|---------|---------|
| `rag` | SQuAD v2 | `rajpurkar/squad_v2` | `main` (*pin a sha before publish*) | CC BY-SA 4.0 | gold-answer facts |
| `chat` | MT-Bench | `HuggingFaceH4/mt_bench_prompts` | `main` (*pin a sha before publish*) | Apache-2.0 | LLM judge |
| `swe` | SWE-bench Lite | `princeton-nlp/SWE-bench_Lite` | `main` (*pin a sha before publish*) | permissive research use | gold-patch paths/symbols facts |
| `code` | HumanEval | `openai/openai_humaneval` | `main` (*pin a sha before publish*) | MIT | judge / opt-in exec pass@1 |
| `reason` | GSM8K | `openai/gsm8k` (`main` config) | `main` (*pin a sha before publish*) | MIT | final-numeric-answer facts |

> Replace *"pin before publish"* with the actual commit sha printed by `datasets` at
> build time, and mirror it into `build_public_dataset.py:PINNED` + `public_dataset.meta.json`.

## Share-alike / attribution notices

- **SQuAD v2 (CC BY-SA 4.0):** © the SQuAD authors (Rajpurkar et al.). Derivative
  distributions of SQuAD text — including `public_dataset.jsonl` when built from SQuAD —
  are licensed **CC BY-SA 4.0**. Attribution: *"Know What You Don't Know: Unanswerable
  Questions for SQuAD"*, Rajpurkar, Jia, Liang (2018).
- **MT-Bench (Apache-2.0):** from LMSYS FastChat. Attribution: *"Judging LLM-as-a-Judge
  with MT-Bench and Chatbot Arena"*, Zheng et al. (2023).
- **SWE-bench Lite:** from *"SWE-bench: Can Language Models Resolve Real-World GitHub
  Issues?"*, Jimenez et al. (2024). The underlying repositories carry their own OSS
  licenses; we ship only issue text + bounded code-context excerpts for measurement.
- **HumanEval (MIT):** © OpenAI. Attribution: *"Evaluating Large Language Models Trained
  on Code"*, Chen et al. (2021).
- **GSM8K (MIT):** © OpenAI. Attribution: *"Training Verifiers to Solve Math Word
  Problems"*, Cobbe et al. (2021).

## Deliberately excluded

- **LMSYS-Chat-1M** — the recognized *real-traffic* dataset (natural repeats, ideal for
  cache realism) is **not** bundled: its click-through **non-commercial** research license
  and residual PII are incompatible with an Apache-2.0, commercially-offered repo. It may
  only ever be an opt-in a verifier fetches and accepts terms for themselves.
