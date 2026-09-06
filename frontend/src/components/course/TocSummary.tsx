import { Lightbulb } from "lucide-react";
import { displayPages, type CourseEstimate } from "@/lib/utils/estimate";

/** Right-hand summary card on the Customize Table of Contents screen. */
export function TocSummary({ estimate }: { estimate: CourseEstimate }) {
  return (
    <aside className="space-y-3">
      <div className="rounded-card border border-line bg-white px-5 py-6 text-center">
        <svg
          viewBox="0 0 120 110"
          className="mx-auto w-[104px]"
          role="img"
          aria-label="Course document illustration"
        >
          <rect
            x="26"
            y="10"
            width="68"
            height="86"
            rx="8"
            fill="#F7E9E3"
            stroke="#E8B7A5"
            strokeWidth="1.5"
          />
          <rect x="38" y="26" width="44" height="5" rx="2.5" fill="#E8B7A5" />
          <rect x="38" y="39" width="36" height="4" rx="2" fill="#F3F0EC" />
          <rect x="38" y="49" width="44" height="4" rx="2" fill="#F3F0EC" />
          <rect x="38" y="59" width="30" height="4" rx="2" fill="#F3F0EC" />
          <rect x="38" y="69" width="40" height="4" rx="2" fill="#F3F0EC" />
          <rect x="38" y="79" width="24" height="4" rx="2" fill="#F3F0EC" />
          <circle cx="28" cy="70" r="7" fill="#C15F3C" />
          <path
            d="M28 66.6v6.8M24.6 70h6.8"
            stroke="#FFFFFF"
            strokeWidth="1.6"
            strokeLinecap="round"
          />
        </svg>

        <p className="mt-5 text-[15px] font-bold text-ink">
          {estimate.chapters} {estimate.chapters === 1 ? "Chapter" : "Chapters"}
        </p>
        <p className="mt-0.5 text-[12.5px] text-ink-500">
          {estimate.sections} {estimate.sections === 1 ? "Section" : "Sections"}
        </p>

        <dl className="mt-6 space-y-4">
          <div>
            <dt className="text-[12px] text-ink-500">Estimated length</dt>
            <dd className="mt-0.5 text-[13px] font-semibold text-ink">
              {displayPages(estimate.pages)}
            </dd>
          </div>
          <div>
            <dt className="text-[12px] text-ink-500">Estimated time</dt>
            <dd className="mt-0.5 text-[13px] font-semibold text-ink">
              {estimate.pages > 0 ? `${estimate.hoursLow}-${estimate.hoursHigh} hours` : "—"}
            </dd>
          </div>
        </dl>
      </div>

      <div className="flex items-start gap-2 rounded-card border border-brand-100 bg-brand-50 px-3.5 py-3">
        <Lightbulb size={14} className="mt-[1px] shrink-0 text-brand-600" aria-hidden />
        <p className="text-[11.5px] leading-[1.5] text-ink-700">
          <span className="font-semibold text-brand-700">Tip:</span> You can drag &amp; drop to
          reorder chapters and sections.
        </p>
      </div>
    </aside>
  );
}
