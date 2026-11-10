/**
 * Minimal ambient types for KaTeX's `contrib/auto-render` sub-export.
 *
 * The `katex` package's own `exports` map only gives a `types` entry for its
 * root `"."` export (see node_modules/katex/package.json) - `./contrib/
 * auto-render` resolves at runtime but has no shipped `.d.ts`, and this repo
 * has no `@types/katex` package to fall back on either. Mirrors the real
 * runtime signature (node_modules/katex/contrib/auto-render/auto-render.ts)
 * just enough for this app's own usage (lib/editor/useMathTypeset.ts) -
 * `Object.assign`s whatever extra options it's given straight through to
 * `katex.render` itself, so `throwOnError` (a real `katex.render` option,
 * just not part of this module's own narrower declared interface upstream)
 * is genuinely honoured at runtime even though it isn't the real module's
 * own named option.
 */
declare module "katex/contrib/auto-render" {
  interface DelimiterSpec {
    left: string;
    right: string;
    display: boolean;
  }

  interface RenderMathInElementOptions {
    delimiters?: DelimiterSpec[];
    ignoredTags?: string[];
    ignoredClasses?: string[];
    errorCallback?: (msg: string, err: Error) => void;
    throwOnError?: boolean;
    displayMode?: boolean;
    macros?: Record<string, string>;
  }

  export default function renderMathInElement(
    elem: HTMLElement,
    options?: RenderMathInElementOptions,
  ): void;
}
