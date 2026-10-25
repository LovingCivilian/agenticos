# Design: ThinkingBlock streaming effects

**One component changes:** `ThinkingBlock` in `frontend/src/components/chat/turn-parts.tsx` (L164, already `export`ed, so the test renders it directly). Summary, `<details>` and all classes stay as they are.

**New structure** (replaces L195-197):
- Scroller: the existing `div.text-muted-foreground … mt-2 max-h-72 overflow-y-auto border-l pl-3.5 text-[13px] leading-relaxed`, now with `ref={scrollerRef}`. Classes unchanged.
- Inner `div ref={contentRef} className={cn(isStreaming && "stream-in")}`: this is the follow content and the `stream-in` wrapper. It wraps `<MarkdownContent content={visible} streaming={flowing} />` directly, as TextBubble does, so `.stream-in > :not(.stream-cursor)` (globals.css L2291) applies. No cursor.

**Hooks** (signatures as in the source files):
- `const visible = useSmoothText(text, isStreaming)`, from `useSmoothText(text: string, streaming: boolean): string`.
- `const flowing = useLinger(isStreaming || visible.length < text.length, WORD_FADE_MS)`, from `useLinger(active: boolean, ms: number): boolean`. `WORD_FADE_MS` (L289) is declared after ThinkingBlock. That is fine because it is read at render, not at module load, so it does not move.
- `useReaderScrolledUp(scrollerRef, scrolledUpRef)`, from `(scroller: RefObject<HTMLElement | null>, scrolledUp: { current: boolean }): void`, where `scrolledUpRef = useRef(false)`.
- `useFollowContent(scrollerRef, contentRef, holdFollow)`, from `(scroller, content: RefObject<HTMLElement | null>, paused?: () => boolean): void`. This is the same pairing as chat-container.tsx L533/L557-558, where `holdFollow = useCallback(() => emptyRef.current || userScrolledUpRef.current, [])`. Here: `holdFollow = useCallback(() => !flowing || scrolledUpRef.current, [flowing])`.
- New import: `import { useCallback, useRef } from "react";`.

**When not streaming**, the result looks the same as today: same scroller classes, an unstyled inner div, no `stream-in`, `streaming={false}` (no `.stream-word` spans), and `visible === text`. Following stays off (`!flowing`). Otherwise the observer's first callback, or opening a finished `<details>`, would jump a finished reasoning to its end.

**Test:** add `frontend/src/components/chat/turn-parts.thinking.test.tsx`. The name follows the `chat-input.mentions.test.tsx` pattern; `turn-parts.test.ts` holds only the pure helpers. It uses vitest and Testing Library. `useTranslations` comes from the English catalog in `vitest.setup.ts`. `MarkdownContent` is loaded with `next/dynamic`, so queries use `findBy`/`waitFor`. The setup file's no-op `MockResizeObserver` is replaced per test with `vi.stubGlobal` and a class that captures its callback, as in `use-follow-content.test.ts`, then `vi.unstubAllGlobals()` in `afterEach`. Scroll geometry is set with `Object.defineProperty(scroller, "scrollHeight", …)`, and the reader scrolls up with `fireEvent.wheel(scroller, { deltaY: -40 })`, as in `use-reader-scrolled-up.test.tsx`. The scroller is found with `container.querySelector(".overflow-y-auto")`.

**CHANGELOG.md**, under `## [Unreleased]`, in a new `### Fixed`: "The chat's reasoning block now streams the way the answer does: words fade in at a steady pace, new paragraphs rise into place, and the box stays scrolled to its newest line unless the reader scrolls up in it."
