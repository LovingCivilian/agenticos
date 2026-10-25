# Implementation plan

1. `turn-parts.tsx` imports: add `import { useCallback, useRef } from "react";` after the `next-intl` import (L4).
2. In `ThinkingBlock` (L164), after `const t = useTranslations("chat")`, add `visible` (useSmoothText), `flowing` (useLinger with WORD_FADE_MS), `scrollerRef`/`contentRef` (`useRef<HTMLDivElement>(null)`), `scrolledUpRef` (`useRef(false)`), `useReaderScrolledUp(scrollerRef, scrolledUpRef)`, `holdFollow` (useCallback on `!flowing || scrolledUpRef.current`) and `useFollowContent(scrollerRef, contentRef, holdFollow)`.
3. JSX L195-197: put `ref={scrollerRef}` on the box div (classes unchanged) and wrap its child in `<div ref={contentRef} className={cn(isStreaming && "stream-in")}>`. Inside that div, render `<MarkdownContent content={visible} streaming={flowing} />`. Add a one-line comment saying why the follow is held once the block stops flowing.
4. CHANGELOG.md: add `### Fixed` under `## [Unreleased]` (L20) with the line from design.md.
5. Add `frontend/src/components/chat/turn-parts.thinking.test.tsx` with these cases:
   - streaming: `.stream-word` spans and the `stream-in` wrapper are present;
   - finished (`isStreaming={false}`): no `.stream-word`, no `stream-in`, and the box keeps `max-h-72 overflow-y-auto`;
   - streaming, content grows (captured RO callback): `scrollTop` becomes the stubbed `scrollHeight`;
   - streaming, after `fireEvent.wheel(scroller, { deltaY: -40 })`, a growth leaves `scrollTop` at 0;
   - finished, a growth leaves `scrollTop` at 0. This case covers the `!flowing` branch for the 100% gate.
6. Verify: `cd frontend && bunx vitest run src/components/chat/turn-parts.thinking.test.tsx src/components/chat/turn-parts.test.ts`, then `make lint-frontend`.

**Ambiguities the tests settle:**
- Follow off when finished: these tests require it (case 5). Holding on `!flowing` instead of `!isStreaming` keeps following through the linger window and any reveal that continues after the stream ends. With `!isStreaming`, the last lines could land unfollowed. Neither test checks that window, so either choice passes, and the choice is a design decision.
- `visible`, not `text`, feeds `MarkdownContent`. While streaming, `useSmoothText` can start behind `text`, so the streaming assertions wait with `findBy`/`waitFor`. Rendering finished from the start makes `visible === text` and `flowing === false` at once, with no timers needed.
- The linger window (700 ms) is not asserted. A streaming-to-finished rerender would need fake timers, and the spec does not require that test.
