# Dataset licenses & attribution

The A/B benchmark (`run_ab.py`) runs items pulled **verbatim** from recognized-standard
public datasets. We invent no question content. This file records each source, its
pinned revision, and its license. The **code** in this repo is Apache-2.0; the **data**
below is distributed under its own license, which coexists with (and is not overridden
by) the code license.

`build_public_dataset.py --hf` reads each corpus at the pinned revision. The checked-in
`public_dataset.jsonl` records its provenance in `public_dataset.meta.json`
(`build_source: "huggingface"` = the real verbatim build; `"fixture"` = a structural
placeholder from the offline sample that must be regenerated from Hugging Face before
publishing any headline number).

| Profile | Dataset | Hugging Face id | Revision | License | Grading |
|---------|---------|-----------------|----------|---------|---------|
| `rag` | HotpotQA (distractor) | `hotpotqa/hotpot_qa` | `main` (*pin a sha before publish*) | CC BY-SA 4.0 | gold-answer facts |
| `chat` | MT-Bench | `HuggingFaceH4/mt_bench_prompts` | `main` (*pin a sha before publish*) | Apache-2.0 | LLM judge |
| `swe` | SWE-bench Lite | `princeton-nlp/SWE-bench_Lite` | `main` (*pin a sha before publish*) | permissive research use | gold-patch paths/symbols facts |
| `code` | HumanEval | `openai/openai_humaneval` | `main` (*pin a sha before publish*) | MIT | judge / opt-in exec pass@1 |
| `reason` | GSM8K | `openai/gsm8k` (`main` config) | `main` (*pin a sha before publish*) | MIT | final-numeric-answer facts |
| `agentic` | BFCL v3 multi_turn | `gorilla-llm/Berkeley-Function-Calling-Leaderboard` | `main` (*pin a sha before publish*) | Apache-2.0 | relative tool-trajectory (proxy vs direct arm) |

> Replace *"pin before publish"* with the actual commit sha printed by `datasets` at
> build time, and mirror it into `build_public_dataset.py:PINNED` + `public_dataset.meta.json`.

## Share-alike / attribution notices

- **HotpotQA (CC BY-SA 4.0):** © the HotpotQA authors (Yang, Qi, Zhang et al.). The
  `distractor` setting ships each question with 10 paragraphs (2 gold + 8 distractors),
  which we stuff verbatim into one multi-document retrieval context — production-realistic
  RAG (~1–2k tokens) so the cold-floor compression lever actually fires. Derivative
  distributions of HotpotQA text — including `public_dataset.jsonl` when built from it —
  are licensed **CC BY-SA 4.0**. Attribution: *"HotpotQA: A Dataset for Diverse,
  Explainable Multi-hop Question Answering"*, Yang, Qi, Zhang, Bengio, Cohen, Salakhutdinov,
  Manning (2018).
- **MT-Bench (Apache-2.0):** from LMSYS FastChat. Attribution: *"Judging LLM-as-a-Judge
  with MT-Bench and Chatbot Arena"*, Zheng et al. (2023).
- **SWE-bench Lite:** from *"SWE-bench: Can Language Models Resolve Real-World GitHub
  Issues?"*, Jimenez et al. (2024). The underlying repositories carry their own OSS
  licenses; we ship only issue text + bounded code-context excerpts for measurement.
- **HumanEval (MIT):** © OpenAI. Attribution: *"Evaluating Large Language Models Trained
  on Code"*, Chen et al. (2021).
- **GSM8K (MIT):** © OpenAI. Attribution: *"Training Verifiers to Solve Math Word
  Problems"*, Cobbe et al. (2021).
- **BFCL v3 multi_turn (Apache-2.0):** Berkeley Function Calling Leaderboard, Gorilla team
  (Patil, Mao et al.), UC Berkeley. Only the **tool schemas** (`multi_turn_func_doc`) and the
  **first user turn** of each task are bundled, verbatim, in `agentic_dataset.jsonl`
  (`build_agentic_dataset.py`). The system prompt is disclosed harness scaffolding and the
  `tool_results` are mocks — the live A/B reproduces only the tool-catalogue-pruning lever
  (G08/G16); it does not attempt tool-output projection (G14/G15), which is not live-reproducible.

## Deliberately excluded

- **LMSYS-Chat-1M** — the recognized *real-traffic* dataset (natural repeats, ideal for
  cache realism) is **not** bundled: its click-through **non-commercial** research license
  and residual PII are incompatible with an Apache-2.0, commercially-offered repo. It may
  only ever be an opt-in a verifier fetches and accepts terms for themselves.
