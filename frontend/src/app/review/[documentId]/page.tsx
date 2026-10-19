import { ReviewScreen } from "@/components/review/ReviewScreen";

export default async function ReviewPage({
  params,
}: {
  params: Promise<{ documentId: string }>;
}) {
  const { documentId } = await params;
  return <ReviewScreen documentId={documentId} />;
}
