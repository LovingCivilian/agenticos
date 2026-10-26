# Implementation plan: let go on a small scroll up

**Hook edits, in order** (`frontend/src/hooks/use-reader-scrolled-up.ts`):
1. Lines 5-6: rewrite the `LET_GO_PX` doc as `/** Further from the foot than this, a scroll up with no gesture seen behind it has let go. */`.
2. Lines 20-22: add "a grabbed scrollbar" to the list of moves up that let go at once.
3. After line 31: add `let grabbing = false;`.
4. Lines 36-46 (`onScroll`): line 43 becomes `fromFoot <= AT_FOOT_PX && top >= lastTop`, and line 44 becomes `top < lastTop && (grabbing || fromFoot > LET_GO_PX)`. Add to the comment above: `// A wheel's scroll is animated and its first frames land a few pixels from the foot - a scroll that moved up there is the reader leaving, so only one that did not move up picks the following back up.`
5. After line 59: add `onPointerDown` (`grabbing = event.target === element;`) and `endGrab` (`grabbing = false;`). Comment: `// A press on the scroller itself, not on anything inside it, is a press on its scrollbar, and while it is held any scroll up lets go. The release is heard on window, capturing: a drag ends wherever the pointer is let go, usually outside the box. A browser that reports no such press keeps the distance rule.`
6. Lines 60-64: add `element.addEventListener("pointerdown", onPointerDown)`, plus `window.addEventListener("pointerup", endGrab, true)` and the same for `"pointercancel"`. Lines 65-71: remove all three, with the same `true` flag on the two `window` listeners.

**CHANGELOG.md:** add `### Fixed` under `## [Unreleased]` (line 20), with the entry in `design.md`.

**Tests** (`use-reader-scrolled-up.test.tsx`). The `Scroller` on line 12 gains `<span data-testid="content" />` inside the scroller. In `at()` geometry, `at(s, 0)` puts `top` at 1500.
- (a) `at(0)`, wheel `deltaY: -20`, then `at(12)` (moved up, 12px from the foot): `state` stays `true`.
- (b) Continue from (a): `at(5)` (moved down) sets `false`. Then wheel up, `fireEvent.scroll` with the geometry unchanged (`top === lastTop` at 5px) sets `false`.
- (c) `at(0)`, `pointerDown(scroller)`, `at(60)`: `true`. Then `pointerUp(document.body)` (released outside the box), `at(0)`, `at(60)`: `false`.
- (c2, needed for coverage and behaviour) The same as (c), but the grab ends with `pointerCancel(document.body)`.
- (d) `at(0)`, `pointerDown(content)`, `at(60)`: `false`.
- (e) `at(0)`, `at(60)`, `at(140)`: `false`. Then `at(200)`: `true` (the 150px rule stays).
- Unmount: the existing test on line 96 and Testing Library's auto-cleanup both run the teardown, so the removal lines are covered. A removed `window` listener cannot be seen through `state`, so no spy test is added for it (assert the consequence, not that a mock was called).
- Branch coverage: (a) and (b) cover both arms of `&&`; (c), (e) and the existing far-drag test cover all three outcomes of `grabbing || fromFoot > LET_GO_PX`.

**Ambiguities the tests settle, or leave open on purpose:**
- `top === lastTop` at the foot **re-arms**. The requirements say `>=`, and (b) pins that.
- A shrink clamp at the foot (`top < lastTop`, `fromFoot` 0) no longer re-arms. The next scroll that does not move up re-arms it. No test.
- A grab that starts already scrolled up changes nothing: the state is already `true`, and a drag back down re-arms through branch 1. No test.
- A mouse press on content (a text-selection drag, whose autoscroll stays on today's rule) is a non-grab. (d) pins it.
- A press on the scroller's own padding counts as a grab, and so does a touch or pen press on it. Touch panning fires `pointercancel`, which ends the grab. Left untested.
- Locally run only `cd frontend && bunx vitest run src/hooks/use-reader-scrolled-up.test.tsx` and `make lint-frontend`.
