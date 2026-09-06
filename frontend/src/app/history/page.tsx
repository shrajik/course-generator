import Link from "next/link";
import { ArrowRight, Clock3, FileClock } from "lucide-react";
import { AdminEmpty } from "@/components/admin/AdminUI";

export default function HistoryPage() {
  return <div className="creator-page mx-auto w-full max-w-[1120px]"><div className="creator-page-heading"><div><p className="creator-eyebrow">Workspace</p><h1 className="creator-page-title">History</h1><p className="creator-page-description">Review generation and editing activity from your courses.</p></div><Clock3 size={22} className="text-brand-500" /></div><section className="creator-content-panel mt-5"><AdminEmpty><span className="creator-empty-icon"><FileClock size={22} /></span><h2 className="text-[15px] font-semibold text-ink">No history to show yet</h2><p className="max-w-sm text-[12px] leading-5 text-ink-500">Course activity is recorded within each course editor. Once you create a course, its saved activity will be available there.</p><Link href="/" className="creator-text-link">Create your first course <ArrowRight size={13} /></Link></AdminEmpty></section></div>;
}
