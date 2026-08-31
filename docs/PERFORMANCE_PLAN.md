# Making course generation fast — plan and results

**Status: Phases 0–3 implemented.** Phase 4 (queue/workers) remains deliberately deferred.

---

## 1. What was wrong

For 8 chapters, ~83% of the wall clock was one thing: **chapters were written strictly one at a time**,
because each writer call received the summaries of the chapters already written. Research, images and PDF
were already parallel or already trivial, so tuning them barely moved the total.

| Phase | Calls | Concurrency | Est. wall clock | Share |
| --- | --- | --- | --- | --- |
| Planner | 1 | — | ~40s | 2% |
| Research (search + structure per chapter) | 16 | 3 | ~5 min | 12% |
| **Write + review (+ revisions)** | **~22** | **1 — serial** | **~33 min** | **83%** |
| Images | 8 | 3 | ~1 min | 2% |
| **Total** | | | **~40 min** | |

The run was also a single blocking HTTP request, so one proxy timeout discarded 40 minutes of work.

---

## 2. What changed

### Phase 0 — Measurement (so this isn't guesswork)

`app/core/metrics.py` times every model call and every phase into a context-local collector. The results
come back on `GenerateResponse.timings`, are persisted on the course record, and are logged as a waterfall:

```
course crs_… finished in 4.85s (13.97 model-seconds across 33 calls, speedup 2.88x, peak 6 concurrent)
  write+review              2.48s  x1
  images                    1.11s  x1
  research                  0.80s  x1
  continuity                0.40s  x1
```

`parallel_speedup` = model-seconds ÷ wall-seconds. It is 1.0 when calls run one after another, and only
exceeds 1 if they genuinely overlap — a direct check that the fan-out is working rather than an assumption.

### Phase 1 — Break the serial chain

**Summary-first fan-out.** The planner already writes a `summary` for every chapter, so the information a
chapter needs about its neighbours exists *before any chapter is written*. Each chapter now receives every
other chapter's summary, split into "already covered — build on it, never repeat it" and "reserved for later
chapters — do not teach these here". No chapter waits for another, so they all run at once. A single cheap
call fills in any summary the planner left blank (usually zero calls).

**Per-phase adaptive concurrency.** `WRITER_CONCURRENCY` / `RESEARCH_CONCURRENCY` / `IMAGE_CONCURRENCY`, each
with its own limiter that **halves itself on a 429 and recovers slowly**. Provider rate limits, not code, are
the ceiling at 8-way fan-out, so raising the limit now degrades instead of failing.

**Background, resumable runs.** `POST /generate` returns a job id immediately (`mode: "sync"` still blocks).
Progress, ETA and timings live on the course record; `GET /api/courses/{id}/run` exposes them. `resume: true`
skips chapters that already have artifacts, so an interrupted run costs only what is left.

**Incremental document save.** The document is rebuilt and saved after *every* chapter, so the editor opens
on the finished chapters while the rest are still being written.

### Phase 2 — Fewer, cheaper calls

- **Single-call research** — the search-enabled call returns the structured artifact directly. 16 calls → 8.
  Falls back to the old two-call path if the model refuses tools + structured output together.
- **Lean reviewer input** — a text-only projection of the chapter instead of full block JSON (ids, styles and
  ~30 mostly-empty content fields per block), capped by `REVIEWER_CONTEXT_CHARS`.
- **Surgical revision** — a review failure rewrites only the flagged blocks, not the whole chapter. A full
  rewrite was the single most expensive thing the pipeline could do.
- **Prompt-cache-friendly ordering** — course context, template and guidance are byte-identical across all N
  chapter calls and now come first, so provider prompt caching can hit them.
- **Right-sized models** — writer/editor stay on the flagship; planner, reviewer and research default to a
  faster tier, with a one-shot fallback to `FALLBACK_MODEL` if a model is unavailable.

### Phase 3 — Perceived speed

- Images run **after** the document is readable and never block editing.
- Deep Research is **per-chapter opt-in** (`deep_research_chapter_ids`) instead of a whole-course mode that
  cost 5–15 minutes per chapter.
- The progress screen shows a **measured ETA** and a **"Start editing"** button as soon as the first chapters
  exist.

---

## 3. Measured

Wall clock is measured with `MOCK_LATENCY_MS`, which gives every simulated model call a fixed cost. That
turns the offline mock client into a way to measure the pipeline's **concurrency** without an API key: if
chapters really are written in parallel, raising the limit makes the run shorter.

8 chapters, every simulated call costing 200ms:

| Writing mode | Writer concurrency | Wall clock | write+review phase |
| --- | --- | --- | --- |
| sequential (old) | 6 | 4.50s | 3.36s |
| parallel | 6 | 2.24s | 0.91s |
| parallel | 8 | **1.85s** | **0.53s** |

**write+review is 6.3× faster; end-to-end is 2.4× faster.**

End-to-end gains less than the phase does because this profile gives every call the same cost, so
write+review is only ~75% of the synthetic total instead of the ~83% it is in reality. All three runs
produced all 8 chapters and a paginated document, and the same suite asserts every image was generated.

Verified in `tests/test_performance.py` (24 checks): parallel beats sequential, calls actually overlap,
raising the limit shortens the phase, topic boundaries reach every chapter, research takes one call per
chapter, revision touches only flagged blocks, background runs complete, resume skips finished chapters, the
document exists before the run ends, and the limiter shrinks on 429.

### Projected on real API latencies

Applying the measured structural changes to the latency model in §1 — **still estimates, not a real run**:

| | Total wall clock | To first editable chapter |
| --- | --- | --- |
| Before | ~40 min | ~40 min |
| After (writer concurrency 6) | ~9 min | ~5 min |
| After (writer concurrency 8) | ~7 min | ~5 min |

The remaining honest caveat: no run has yet gone through the real API. `GenerateResponse.timings` will say
exactly where the time went on the first real course, and §5 lists what to do with that.

---

## 4. Trade-offs taken

- **Parallel writing trades a real previous-chapter summary for the planned one.** If a chapter drifts from
  its plan, its neighbours were told something slightly wrong. Mitigations shipped: explicit
  "reserved for later chapters" boundaries in every prompt, real summaries substituted for planned ones
  whenever a neighbour has already finished, and a one-call **continuity pass** over the whole course that
  reports repetition, gaps, transitions and ordering as warnings. `WRITING_MODE=sequential` restores the old
  behaviour so the two can be A/B'd on the same TOC before trusting the default.
- **The continuity pass reports, it does not rewrite.** Auto-editing finished chapters from a whole-course
  judgement is a good way to make a course worse; the findings surface as warnings instead.
- **Faster models for planner/reviewer/research** trade a little judgement quality for a large latency win.
  The per-agent env vars make this reversible in one line.
- **Higher concurrency will hit rate limits.** That is why the limiter shrinks on 429 rather than assuming
  headroom.

## 5. What is still on the table

1. **Pipeline research into writing instead of a barrier.** Research currently completes for all chapters
   before writing starts, which is what keeps time-to-first-chapter at ~5 min. Writing chapter *k* as soon as
   *its own* research lands would cut that to roughly one chapter's own pipeline.
2. **Split long chapters into two parallel calls** along template section groups (concept ‖ practice).
3. **Stream partial chapter content** into the editor rather than waiting for the whole structured response.
4. **Phase 4: a real queue** — for parallelism across machines, cancellation and durable retries. Worth doing
   when there are concurrent users, not to fix this.
