"use client";

/**
 * Editor state: the current Course Document plus selection and an undo/redo
 * history. The backend stays the persisted source of truth; this holds the
 * working copy while the user edits.
 *
 *   Backend -> Course Document JSON -> editor state -> user/AI changes -> backend
 *
 * History is a stack of whole-document snapshots. That is more than enough for
 * an MVP document of this size and makes AI patches undoable for free.
 */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useReducer,
  type Dispatch,
} from "react";
import type {
  Block,
  BlockLayout,
  BlockStyle,
  BlockType,
  CourseDocument,
  DocumentPatch,
} from "@/lib/types/document";
import { applyPatch, type PatchResult } from "./patch";
import { createBlock, locateBlock, setByPath } from "./blocks";

const HISTORY_LIMIT = 60;

export interface EditorState {
  document: CourseDocument | null;
  /** Last version fetched from the backend, used to detect local divergence. */
  serverVersion: number;
  past: CourseDocument[];
  future: CourseDocument[];
  selectedIds: string[];
  activePageIndex: number;
  editingBlockId: string | null;
  dirty: boolean;
}

type Action =
  | { type: "load"; document: CourseDocument }
  /** Replace the document with an authoritative server copy, keeping history. */
  | { type: "sync"; document: CourseDocument }
  /** Adopt the server copy without adding a history entry (post-patch reconcile). */
  | { type: "reconcile"; document: CourseDocument }
  /** Push the current document onto the undo stack before a mutation burst. */
  | { type: "checkpoint" }
  /** Replace the document without touching history (used during drags). */
  | { type: "set"; document: CourseDocument }
  | { type: "select"; ids: string[] }
  | { type: "page"; index: number }
  | { type: "editing"; id: string | null }
  | { type: "undo" }
  | { type: "redo" };

const INITIAL: EditorState = {
  document: null,
  serverVersion: 0,
  past: [],
  future: [],
  selectedIds: [],
  activePageIndex: 0,
  editingBlockId: null,
  dirty: false,
};

function clampPage(document: CourseDocument | null, index: number): number {
  if (!document || document.pages.length === 0) return 0;
  return Math.max(0, Math.min(index, document.pages.length - 1));
}

function reducer(state: EditorState, action: Action): EditorState {
  switch (action.type) {
    case "load":
      return {
        ...INITIAL,
        document: action.document,
        serverVersion: action.document.version,
      };

    case "sync":
      return {
        ...state,
        document: action.document,
        serverVersion: action.document.version,
        past: state.document
          ? [...state.past, state.document].slice(-HISTORY_LIMIT)
          : state.past,
        future: [],
        dirty: false,
        editingBlockId: null,
        activePageIndex: clampPage(action.document, state.activePageIndex),
        selectedIds: state.selectedIds.filter((id) => locateBlock(action.document, id)),
      };

    case "reconcile":
      return {
        ...state,
        document: action.document,
        serverVersion: action.document.version,
        dirty: false,
        editingBlockId: null,
        activePageIndex: clampPage(action.document, state.activePageIndex),
        selectedIds: state.selectedIds.filter((id) => locateBlock(action.document, id)),
      };

    case "checkpoint":
      if (!state.document) return state;
      return {
        ...state,
        past: [...state.past, state.document].slice(-HISTORY_LIMIT),
        future: [],
      };

    case "set":
      return {
        ...state,
        document: action.document,
        dirty: true,
        activePageIndex: clampPage(action.document, state.activePageIndex),
      };

    case "select":
      return { ...state, selectedIds: action.ids, editingBlockId: null };

    case "page":
      return {
        ...state,
        activePageIndex: clampPage(state.document, action.index),
        editingBlockId: null,
      };

    case "editing":
      return { ...state, editingBlockId: action.id };

    case "undo": {
      if (state.past.length === 0 || !state.document) return state;
      const previous = state.past[state.past.length - 1];
      return {
        ...state,
        document: previous,
        past: state.past.slice(0, -1),
        future: [state.document, ...state.future].slice(0, HISTORY_LIMIT),
        dirty: previous.version !== state.serverVersion,
        editingBlockId: null,
        activePageIndex: clampPage(previous, state.activePageIndex),
        selectedIds: state.selectedIds.filter((id) => locateBlock(previous, id)),
      };
    }

    case "redo": {
      if (state.future.length === 0 || !state.document) return state;
      const next = state.future[0];
      return {
        ...state,
        document: next,
        past: [...state.past, state.document].slice(-HISTORY_LIMIT),
        future: state.future.slice(1),
        dirty: next.version !== state.serverVersion,
        editingBlockId: null,
        activePageIndex: clampPage(next, state.activePageIndex),
        selectedIds: state.selectedIds.filter((id) => locateBlock(next, id)),
      };
    }

    default:
      return state;
  }
}

interface EditorContextValue {
  state: EditorState;
  dispatch: Dispatch<Action>;
}

const EditorContext = createContext<EditorContextValue | null>(null);

export function EditorProvider({ children }: { children: React.ReactNode }) {
  const [state, dispatch] = useReducer(reducer, INITIAL);
  const value = useMemo(() => ({ state, dispatch }), [state]);
  return <EditorContext.Provider value={value}>{children}</EditorContext.Provider>;
}

function useEditorContext(): EditorContextValue {
  const context = useContext(EditorContext);
  if (!context) throw new Error("useEditor must be used inside EditorProvider");
  return context;
}

export interface EditorApi extends EditorState {
  canUndo: boolean;
  canRedo: boolean;
  activePage: CourseDocument["pages"][number] | null;
  selectedBlocks: Block[];

  load: (document: CourseDocument) => void;
  sync: (document: CourseDocument) => void;
  reconcile: (document: CourseDocument) => void;
  select: (ids: string[]) => void;
  setPage: (index: number) => void;
  setEditing: (id: string | null) => void;
  undo: () => void;
  redo: () => void;
  checkpoint: () => void;

  /** Mutations. `transient` skips the history entry (drag in progress). */
  updateContentPath: (blockId: string, path: string, value: unknown) => void;
  updateContent: (blockId: string, patch: Record<string, unknown>) => void;
  updateStyle: (blockId: string, patch: BlockStyle) => void;
  updateLayout: (blockId: string, patch: Partial<BlockLayout>, transient?: boolean) => void;
  deleteBlock: (blockId: string) => void;
  insertBlock: (type: BlockType, afterBlockId?: string | null) => void;
  addPage: () => void;
  applyDocumentPatch: (patch: DocumentPatch) => PatchResult | null;
}

export function useEditor(): EditorApi {
  const { state, dispatch } = useEditorContext();
  const { document } = state;

  const mutate = useCallback(
    (mutator: (draft: CourseDocument) => void, options: { transient?: boolean } = {}) => {
      if (!state.document) return;
      if (!options.transient) dispatch({ type: "checkpoint" });
      const draft = structuredClone(state.document);
      mutator(draft);
      dispatch({ type: "set", document: draft });
    },
    [dispatch, state.document],
  );

  const updateContentPath = useCallback(
    (blockId: string, path: string, value: unknown) => {
      mutate((draft) => {
        const found = locateBlock(draft, blockId);
        if (!found) return;
        const block = draft.pages[found.pageIndex].blocks[found.blockIndex];
        block.content = setByPath(block.content, path, value);
        block.meta = { ...block.meta, origin: "edited" };
      });
    },
    [mutate],
  );

  const updateContent = useCallback(
    (blockId: string, patch: Record<string, unknown>) => {
      mutate((draft) => {
        const found = locateBlock(draft, blockId);
        if (!found) return;
        const block = draft.pages[found.pageIndex].blocks[found.blockIndex];
        block.content = { ...block.content, ...patch };
        block.meta = { ...block.meta, origin: "edited" };
      });
    },
    [mutate],
  );

  const updateStyle = useCallback(
    (blockId: string, patch: BlockStyle) => {
      mutate((draft) => {
        const found = locateBlock(draft, blockId);
        if (!found) return;
        const block = draft.pages[found.pageIndex].blocks[found.blockIndex];
        block.style = { ...block.style, ...patch };
      });
    },
    [mutate],
  );

  const updateLayout = useCallback(
    (blockId: string, patch: Partial<BlockLayout>, transient = false) => {
      mutate(
        (draft) => {
          const found = locateBlock(draft, blockId);
          if (!found) return;
          const block = draft.pages[found.pageIndex].blocks[found.blockIndex];
          block.layout = { ...block.layout, ...patch };
        },
        { transient },
      );
    },
    [mutate],
  );

  const deleteBlock = useCallback(
    (blockId: string) => {
      mutate((draft) => {
        const found = locateBlock(draft, blockId);
        if (!found) return;
        draft.pages[found.pageIndex].blocks.splice(found.blockIndex, 1);
      });
      dispatch({ type: "select", ids: [] });
    },
    [dispatch, mutate],
  );

  const insertBlock = useCallback(
    (type: BlockType, afterBlockId?: string | null) => {
      if (!state.document) return;
      const block = createBlock(type);
      mutate((draft) => {
        const anchor = afterBlockId ? locateBlock(draft, afterBlockId) : null;
        if (anchor) {
          block.layout = {
            ...block.layout,
            y: anchor.block.layout.y + anchor.block.layout.height + 18,
          };
          block.meta = {
            ...block.meta,
            chapter_id: anchor.block.meta.chapter_id ?? null,
            chapter_number: anchor.block.meta.chapter_number ?? null,
          };
          draft.pages[anchor.pageIndex].blocks.splice(anchor.blockIndex + 1, 0, block);
          return;
        }
        const page = draft.pages[state.activePageIndex];
        if (!page) return;
        const last = page.blocks[page.blocks.length - 1];
        block.layout = { ...block.layout, y: last ? last.layout.y + last.layout.height + 18 : 72 };
        page.blocks.push(block);
      });
      dispatch({ type: "select", ids: [block.id] });
    },
    [dispatch, mutate, state.activePageIndex, state.document],
  );

  const addPage = useCallback(() => {
    if (!state.document) return;
    const nextNumber = state.document.pages.length + 1;
    mutate((draft) => {
      draft.pages.push({
        id: `page_${nextNumber}`,
        page_number: nextNumber,
        size: draft.pages[0]?.size ?? { width: 794, height: 1123 },
        background: draft.pages[0]?.background ?? "#ffffff",
        kind: "content",
        blocks: [],
      });
    });
    dispatch({ type: "page", index: nextNumber - 1 });
  }, [dispatch, mutate, state.document]);

  const applyDocumentPatch = useCallback(
    (patch: DocumentPatch): PatchResult | null => {
      if (!state.document) return null;
      const result = applyPatch(state.document, patch);
      if (result.applied.length > 0) {
        dispatch({ type: "checkpoint" });
        dispatch({ type: "set", document: result.document });
      }
      return result;
    },
    [dispatch, state.document],
  );

  const activePage = document ? (document.pages[state.activePageIndex] ?? null) : null;

  const selectedBlocks = useMemo(() => {
    if (!document) return [];
    return state.selectedIds
      .map((id) => locateBlock(document, id)?.block)
      .filter((block): block is Block => Boolean(block));
  }, [document, state.selectedIds]);

  return {
    ...state,
    canUndo: state.past.length > 0,
    canRedo: state.future.length > 0,
    activePage,
    selectedBlocks,
    load: useCallback((doc) => dispatch({ type: "load", document: doc }), [dispatch]),
    sync: useCallback((doc) => dispatch({ type: "sync", document: doc }), [dispatch]),
    reconcile: useCallback(
      (doc) => dispatch({ type: "reconcile", document: doc }),
      [dispatch],
    ),
    select: useCallback((ids) => dispatch({ type: "select", ids }), [dispatch]),
    setPage: useCallback((index) => dispatch({ type: "page", index }), [dispatch]),
    setEditing: useCallback((id) => dispatch({ type: "editing", id }), [dispatch]),
    undo: useCallback(() => dispatch({ type: "undo" }), [dispatch]),
    redo: useCallback(() => dispatch({ type: "redo" }), [dispatch]),
    checkpoint: useCallback(() => dispatch({ type: "checkpoint" }), [dispatch]),
    updateContentPath,
    updateContent,
    updateStyle,
    updateLayout,
    deleteBlock,
    insertBlock,
    addPage,
    applyDocumentPatch,
  };
}
