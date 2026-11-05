import { OutputWorkspace } from "@/components/editor/OutputWorkspace";

export default async function WorkspacePage({
  params,
}: {
  params: Promise<{ documentId: string }>;
}) {
  const { documentId } = await params;
  return <OutputWorkspace documentId={documentId} />;
}
