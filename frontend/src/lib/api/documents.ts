import { apiUrl, request } from "./client";
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

/** Triggers the render without transferring the PDF body - a small JSON
 * response (`{ size_bytes, ... }`), reliable even for a 10+ MB course where
 * downloading the body straight through `fetch()` can have its connection
 * drop mid-transfer (CourseEditor's handleExport pairs this with
 * `exportedPdfDownloadUrl` below for a native, far-more-resilient browser
 * download instead of buffering the whole file through JS). */
export function generateExportedPdf(documentId: string): Promise<{ size_bytes: number }> {
  return request(`/api/documents/${documentId}/export/pdf`, {
    method: "POST",
    query: { download: false },
  });
}

/** The already-exported PDF (see generateExportedPdf, which must be called
 * first - this never regenerates it) as a plain GET URL, meant for a
 * native browser download (an `<a href>` click/`window.open`), not fetch -
 * the OS/browser download manager streams straight to disk and tolerates a
 * mid-transfer connection hiccup fetch().blob() cannot. */
export function exportedPdfDownloadUrl(documentId: string): string {
  return apiUrl(`/api/documents/${documentId}/export/pdf/download`);
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
