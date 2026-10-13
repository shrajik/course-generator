import assert from "node:assert/strict";
import { test } from "node:test";

import { summarizeModelUsage } from "./modelUsage.ts";

import type { AdminModelUsageRecord } from "../api/admin.ts";

function call(overrides: Partial<AdminModelUsageRecord> = {}): AdminModelUsageRecord {
  return {
    kind: "structured",
    purpose: "writer:chapter_1",
    model: "gpt-5",
    provider: "azure",
    seconds: 1.2,
    input_tokens: 100,
    output_tokens: 50,
    retries: 0,
    failed: false,
    used_fallback: false,
    timestamp: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

test("an empty call list summarizes to all zeros", () => {
  assert.deepEqual(summarizeModelUsage([]), {
    totalCalls: 0,
    totalInputTokens: 0,
    totalOutputTokens: 0,
    failedCount: 0,
    fallbackCount: 0,
  });
});

test("tokens, failures and fallbacks are summed across every call", () => {
  const calls = [
    call({ input_tokens: 100, output_tokens: 50 }),
    call({ input_tokens: 200, output_tokens: 80, failed: true }),
    call({ input_tokens: 10, output_tokens: 5, used_fallback: true }),
    call({ input_tokens: 10, output_tokens: 5, failed: true, used_fallback: true }),
  ];
  assert.deepEqual(summarizeModelUsage(calls), {
    totalCalls: 4,
    totalInputTokens: 320,
    totalOutputTokens: 140,
    failedCount: 2,
    fallbackCount: 2,
  });
});

test("a single successful call is counted once with no failures or fallbacks", () => {
  const summary = summarizeModelUsage([call()]);
  assert.equal(summary.totalCalls, 1);
  assert.equal(summary.failedCount, 0);
  assert.equal(summary.fallbackCount, 0);
});
