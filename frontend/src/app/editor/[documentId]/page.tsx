import { CourseEditor } from "@/components/editor/CourseEditor";
import { EditorProvider } from "@/lib/editor/store";

export default async function EditorPage({
  params,
}: {
  params: Promise<{ documentId: string }>;
}) {
  const { documentId } = await params;
  return (
    <EditorProvider>
      <CourseEditor documentId={documentId} />
    </EditorProvider>
  );
}
