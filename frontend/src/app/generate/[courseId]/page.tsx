import { GenerationProgress } from "@/components/generation/GenerationProgress";

export default async function GeneratePage({
  params,
}: {
  params: Promise<{ courseId: string }>;
}) {
  const { courseId } = await params;
  return (
    <main className="mx-auto w-full max-w-[1320px] px-4 py-8 sm:px-6">
      <GenerationProgress courseId={courseId} />
    </main>
  );
}
