# EASR: Effect-Aware Selective Repair for Web Agents

Anonymous code release for the paper *Saving More Than Intended: Effect-Aware Selective Repair for Web Agents*.

EASR is a browser-side runtime controller for web agents. At every commit (a save), it captures the outgoing request before it
leaves the browser. It then builds an operation contract that separates the fields the agent intended to change from everything the
save will actually write. It re-reads the record and the task's decision dependencies in an independent session, and it classifies
the commit as benign, a decision conflict, an effect-only conflict, or both. On an effect-only conflict it rewrites the agent's own
request (Rebase). On a decision conflict it holds the save and names the changed facts (LocalReplan). After every forwarded save it
verifies the outcome. No stage calls a language model.

## Repository layout

```
EASR/                       Python package (method, baselines, harness)
  easr.py                   EASR controller: operation contract, conflict classes, Rebase / LocalReplan / SafeHold,
                            post-commit VerifyOutcome (paper Section 3, Algorithm 1)
  controllers.py            registry of every compared method: B0-B7, EASR, ablations A1-A9,
                            contract-quality variants, application-integrated (Tier B) client
  guards.py                 shared pre-commit validator used by B2, B3, B5 and B7
  apps/                     application adapters: commit detection, request capture and rewriting, footprint scripts,
                            evaluator-side reads/writes and intervention hooks
    oscar.py  crm.py  breakingweb.py  mantis.py  common.py
  easr_eval.py              the 48 operations, intervention classes, manifest builder and operation-level oracle
  easr_supplement.py        write-breadth, task-length (6/8 subtasks) and interaction studies
  easr_study.py             scripted, model-free controller study (Appendix A.5)
  runner.py  worker.py      episode loop and cohort workers (Playwright, one browser per worker)
  actions.py observe.py prompts.py llm.py   agent interface: action space, text observation, prompts, OpenAI-style client
  dynamics.py               event schedule of the concurrent "other party"
  repair.py guard.py ledger.py oracle.py facts.py tasks.py cohort.py   harness modules imported by the worker
apps/                       build recipes and world layers of the four self-hosted applications
  oscar/                    django-oscar 4.0 shop (Django project, world middleware, templates)
  crm/                      SuiteCRM 7.14.9 (silent-install config, seed, world hooks, private MariaDB)
  mantis/                   MantisBT 2.28.4 (seed, world layer, Tier B layer)
  breakingweb/              evaluator-only state and fault endpoints around the BreakingWeb backend
  setup/install_envs.sh     builds the Python environments and downloads MariaDB, PHP and SuiteCRM
tasks/                      easr_eval_v1.json (methods and their switches), dev_v0.json (base tasks), fixtures/
protocol/easr_eval_v1.json  run configuration (decoding, step/time budgets, observation size)
bin/                        runtime setup, model server, cohort launcher, one pipeline script per experiment
tests/easr/                 model-free checks cited in Appendix A.4 (trigger reached, write breadth)
analysis/easr_eval_report.py  paired estimator: mean over operations, 95% CI by bootstrap over operations (5,000, seed 7)
```

## Paper to code

| Paper | Experiment | Pipeline | Cohorts written under `results/easr/` |
|---|---|---|---|
| Table 2, Figure 2 (RQ1) | request footprint at every EASR save | `bin/run_easr_matrix.sh` | `main_<agent>` |
| Tables 3-6, 13, 14; Figure 3 (RQ2) | main matrix: 9 methods x 5 agents x 339 tasks | `bin/run_easr_matrix.sh` | `validate_<agent>`, `main_<agent>` |
| Tables 7, 15 (RQ3) | ablations A1-A9 | `bin/run_easr_ablations.sh` | `ablation_<agent>` |
| Table 8 | degraded contracts (CQ_*) | `bin/run_easr_extras.sh` | `contract_<agent>` |
| Table 9 | injected faults (silent failure, partial commit) | `bin/run_easr_extras.sh` | `faults_<agent>` |
| Table 10 | application-integrated references (Tier B) | `bin/run_easr_tierb.sh` | `tierb_<agent>` |
| Table 16, Figure 5 (timing) | timing, intensity, repeated changes | `bin/run_easr_sensitivity.sh` | `sens_timing_intensity_<agent>`, `e8_repeated_<agent>` |
| Table 17 | multi-step tasks (2-4 / 6-8 subtasks) | `bin/run_easr_sensitivity.sh`, `bin/run_easr_supplement.sh` | `e7_composite_<agent>`, `sens_length{6,8}_<agent>` |
| Table 18, Figure 5 (breadth) | write breadth and interaction | `bin/run_easr_supplement.sh` | `sens_breadth_<agent>`, `sens_interaction_<agent>` |
| Figure 4 | safety-availability trade-off | main, ablation, contract and Tier B pipelines | as above |
| Table 12 | scripted controller study (18 controllers) | `python -m pair.easr_study run OUT SLOT` | any folder |

The fixed version EASR v2.1.1 in Table 9 is the arm `EASR_v2_1_1`. Run it on the fault classes with
`python -m pair.easr_eval manifest results/easr/faults_<agent>_v211 --no-clean --arms EASR_v2_1_1 --classes F_silent_fail,F_partial_commit --ops <BreakingWeb operations> ...`.

## Requirements

- Linux x86_64, Python 3.12, `curl`, `unzip`, and the system libraries headless Chromium needs.
- Two GPUs for the vLLM model server. The paper used two RTX 5090s with tensor parallelism 2, one model at a time.
- Third-party applications, which are not redistributed here:
  - BreakingWeb (public benchmark, https://www.breakingweb.app/), checked out at `external/BreakingWeb` with its own `.venv`,
    or set `BREAKINGWEB_DIR`.
  - MantisBT 2.28.4 source at `external/mantisbt-2.28.4`, or set `MANTIS_SRC`.
  - SuiteCRM 7.14.9, MariaDB 11.4.13 and a static PHP 8.2 CLI are downloaded by `apps/setup/install_envs.sh`.
- Model checkpoints, one folder per model under `$MODELS_DIR`:

  | folder | Hugging Face model | role in `bin/serve_model.sh` |
  |---|---|---|
  | `Qwen3.8-27B-FP8` | Qwen/Qwen3.8-27B-FP8 | `actor` (port 18500) |
  | `gemma-4-12B-it` | google/gemma-4-12B-it | `actor2` (port 18502) |
  | `phi-4` | microsoft/phi-4 | `phi4` (port 18504) |
  | `Mistral-Nemo-Instruct-2407` | mistralai/Mistral-Nemo-Instruct-2407 | `mistral` (port 18505) |
  | `granite-3.3-8b-instruct` | ibm-granite/granite-3.3-8b-instruct | `granite` (port 18503) |

## Setup

```bash
apps/setup/install_envs.sh all        # env/vllm, env/tools (Playwright + Chromium), env/oscar, MariaDB, PHP, SuiteCRM
bin/setup_runtime.sh                  # MariaDB, then the shop, CRM and MantisBT templates (built once, then restored)
bin/start_breakingweb.sh              # BreakingWeb backend with the evaluator-only endpoints on 127.0.0.1:18700
```

Application databases live under `$PAIR_RT` (default `/tmp/pair_rebuild_rt`). Each episode starts from a fresh copy of its template.

## Running

One cohort by hand:

```bash
. bin/env.sh
MODELS_DIR=/path/to/checkpoints bin/serve_model.sh actor
env/tools/bin/python -m pair.easr_eval manifest results/easr/demo \
    --arms B0_vanilla,B7_repair_v1,EASR_v2 --classes C_untouched,B_dependency --ops C1_opportunity_stage,BW1_gmail_page_size \
    --endpoint-url http://127.0.0.1:18500/v1/chat/completions --model pair-actor --actor Qwen3.8-27B-FP8
bin/run_cohort.sh results/easr/demo 4            # 4 parallel workers; wait until they exit
env/tools/bin/python -m pair.easr_eval evaluate results/easr/demo
python3 analysis/easr_eval_report.py results/easr/demo_report results/easr/demo
```

Whole experiments: run the pipeline scripts in the table above with `MODELS_DIR` set. Each script starts and stops the model
servers, builds missing cohorts, runs them with 10 workers, and evaluates them. For example:

```bash
MODELS_DIR=/path/to/checkpoints bin/run_easr_matrix.sh qwen gemma phi4 mistral granite
```

Model-free checks (no model server needed):

```bash
. bin/env.sh
env/tools/bin/python tests/easr/trigger_probe.py      # every operation's trigger is reached and the watched fields are readable
env/tools/bin/python tests/easr/breadth_probe.py      # a plain save overwrites a concurrent change exactly when the field is on the form
```

## Outputs

Each cohort folder holds `manifest.json` (frozen assignments), `index.jsonl` (one line per episode),
`episodes/<id>/{assignment.json, steps.jsonl, episode.json, requests.jsonl.gz}`, and, after evaluation, `easr_eval.jsonl`. The
oracle in `pair/easr_eval.py` defines `intended_ok`, `preserved`, `blind`, `stale`, `unsafe` and `correct_and_safe`, together with
hold and cost counts.
