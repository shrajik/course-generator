/**
 * Pure aggregation over a course's per-call AI model usage (see
 * AdminModelUsageRecord in lib/api/admin.ts) - kept separate from the page
 * component so it's unit-testable without any React rendering.
 */

import type { AdminModelUsageRecord } from "@/lib/api/admin";

export interface ModelUsageSummary {
  totalCalls: number;
  totalInputTokens: number;
  totalOutputTokens: number;
  failedCount: number;
  fallbackCount: number;
}

export function summarizeModelUsage(calls: AdminModelUsageRecord[]): ModelUsageSummary {
  return calls.reduce<ModelUsageSummary>(
    (summary, call) => ({
      totalCalls: summary.totalCalls + 1,
      totalInputTokens: summary.totalInputTokens + call.input_tokens,
      totalOutputTokens: summary.totalOutputTokens + call.output_tokens,
      failedCount: summary.failedCount + (call.failed ? 1 : 0),
      fallbackCount: summary.fallbackCount + (call.used_fallback ? 1 : 0),
    }),
    { totalCalls: 0, totalInputTokens: 0, totalOutputTokens: 0, failedCount: 0, fallbackCount: 0 },
  );
}
