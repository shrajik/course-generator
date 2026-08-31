import { Sparkles } from "lucide-react";

/** The illustrated left-hand panel of the Create Course screen. */
export function CoursePreviewArt() {
  return (
    <div className="flex h-full flex-col items-center justify-between rounded-card border border-line bg-[#F7F6FE] px-5 py-7 text-center">
      <svg
        viewBox="0 0 180 190"
        className="w-[150px]"
        role="img"
        aria-label="Illustration of an AI-generated course document"
      >
        <ellipse cx="90" cy="96" rx="66" ry="66" fill="#EDE9FE" />
        <g stroke="#C4B4FC" strokeWidth="1.4" strokeLinecap="round">
          <path d="M20 66h10M18 96h8M22 126h10M150 66h10M154 96h8M148 126h10" />
        </g>
        <rect
          x="46"
          y="34"
          width="88"
          height="122"
          rx="10"
          fill="#FFFFFF"
          stroke="#DDD5FE"
          strokeWidth="1.5"
        />
        <rect x="58" y="48" width="26" height="8" rx="4" fill="#DDD5FE" />
        <circle cx="63" cy="70" r="6" fill="#C4B4FC" />
        <rect x="74" y="66" width="46" height="4" rx="2" fill="#EDE9FE" />
        <rect x="74" y="74" width="32" height="4" rx="2" fill="#EDE9FE" />
        <rect
          x="58"
          y="90"
          width="62"
          height="34"
          rx="6"
          fill="#F6F4FE"
          stroke="#DDD5FE"
          strokeWidth="1.2"
        />
        <path d="M70 114l10-12 8 9 7-7 13 12" stroke="#A78BFA" strokeWidth="1.6" fill="none" />
        <circle cx="76" cy="99" r="3.4" fill="#C4B4FC" />
        <rect x="58" y="132" width="52" height="4" rx="2" fill="#EDE9FE" />
        <rect x="58" y="141" width="38" height="4" rx="2" fill="#EDE9FE" />
        <g fill="#8B6DF6">
          <path d="M132 26l2.2 5 5 2.2-5 2.2-2.2 5-2.2-5-5-2.2 5-2.2z" />
          <path d="M44 150l1.6 3.6 3.6 1.6-3.6 1.6-1.6 3.6-1.6-3.6-3.6-1.6 3.6-1.6z" />
        </g>
      </svg>

      <p className="mt-6 max-w-[190px] text-[12.5px] leading-relaxed text-ink-500">
        AI will research, write and create an interactive course based on your inputs.
      </p>

      <Sparkles size={16} className="mt-6 text-brand-500" />
    </div>
  );
}
