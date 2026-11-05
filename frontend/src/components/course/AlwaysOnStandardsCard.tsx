"use client";

/**
 * Purely informational - lists the checks app.course.compliance already
 * runs unconditionally for every course, regardless of what's configured in
 * the Packaging/Accessibility/Metadata/Writing Style panels above. No
 * props, no state, nothing sent to the backend - this never needs to stay
 * in sync with a schema, since it describes behaviour that already exists
 * and never changes based on a selection here.
 */

const ITEMS = [
  {
    title: "Quality assurance self-check",
    description: "Quality Matters–style review across 8 checks. Content that fails is fixed or flagged, never shown as final.",
  },
  {
    title: "Information Mapping",
    description: "Every chunk typed (Procedure, Process, Concept, Fact, Structure, Classification, Principle) before it's written.",
  },
  {
    title: "SME verification flags",
    description: "Technical claims, figures, procedures and policy statements are marked inline for a human reviewer.",
  },
];

export function AlwaysOnStandardsCard() {
  return (
    <div className="rounded-card border border-line bg-canvas px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <span className="text-[12.5px] font-medium text-ink-700">Always on</span>
        <span className="rounded-full border border-line bg-white px-2 py-0.5 text-[10.5px] font-medium text-ink-400">
          Not configurable
        </span>
      </div>
      <p className="mt-1 text-[11.5px] text-ink-400">
        Run on every deliverable regardless of the choices above.
      </p>
      <ul className="mt-3 grid gap-2 sm:grid-cols-3">
        {ITEMS.map((item) => (
          <li key={item.title} className="rounded-lg border border-line bg-white px-3 py-2">
            <span className="block text-[12px] font-medium text-ink-800">{item.title}</span>
            <span className="block text-[11px] leading-snug text-ink-500">{item.description}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
