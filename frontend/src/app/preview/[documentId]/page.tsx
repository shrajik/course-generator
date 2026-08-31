import { CoursePreview } from "@/components/editor/CoursePreview";

export default async function PreviewPage({
  params,
}: {
  params: Promise<{ documentId: string }>;
}) {
  const { documentId } = await params;
  return <CoursePreview documentId={documentId} />;
}
