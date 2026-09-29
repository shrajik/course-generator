import { apiUrl, request, requestBlob } from "./client";
import type {
  AiEditRequest,
  AiEditResponse,
  ConceptVisualSpec,
  CourseDocument,
  UpdateConceptVisualSpecResponse,
} from "@/lib/types/document";

export function getDocument(documentId: string, signal?: AbortSignal): Promise<CourseDocument> {
  return request<CourseDocument>(`/api/documents/${documentId}`, { signal });
}

export function getCourseDocument(courseId: string): Promise<CourseDocument> {
  return request<CourseDocument>(`/api/courses/${courseId}/document`);
}

/** Persist manual editor edits (move/resize/type/insert/delete/style). */
export function saveDocument(
  documentId: string,
  document: CourseDocument,
): Promise<CourseDocument> {
  return request<CourseDocument>(`/api/documents/${documentId}`, {
    method: "PUT",
    body: document,
  });
}

export function aiEdit(
  documentId: string,
  payload: AiEditRequest,
): Promise<AiEditResponse> {
  return request<AiEditResponse>(`/api/documents/${documentId}/ai-edit`, {
    method: "POST",
    body: {
      apply: true,
      regenerate_images: true,
      ...payload,
    },
  });
}

/** Fix a label/description inside a concept_experience visual and get back
 * the re-rendered block - no AI call, instant, and never touches the
 * exported PDF's static rendering contract. */
export function updateConceptVisualSpec(
  documentId: string,
  blockId: string,
  spec: ConceptVisualSpec,
): Promise<UpdateConceptVisualSpecResponse> {
  return request<UpdateConceptVisualSpecResponse>(
    `/api/documents/${documentId}/blocks/${blockId}/visual-spec`,
    { method: "PUT", body: { spec } },
  );
}

/** The backend renders the PDF (HTML -> CSS -> Playwright). We only download it. */
export function exportPdf(documentId: string): Promise<Blob> {
  return requestBlob(`/api/documents/${documentId}/export/pdf`, { method: "POST" });
}

/** API-relative path for a document-relative asset such as "assets/image_001.png". */
export function assetPath(documentId: string, path: string | null | undefined): string | null {
  if (!path) return null;
  const name = path.split("/").pop();
  if (!name) return null;
  return `/api/documents/${documentId}/assets/${name}`;
}

/** Resolve a document-relative asset path such as "assets/image_001.png". */
export function assetUrl(documentId: string, path: string | null | undefined): string | null {
  const relative = assetPath(documentId, path);
  return relative ? apiUrl(relative) : null;
}
