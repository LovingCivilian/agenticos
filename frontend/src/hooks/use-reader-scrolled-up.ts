"use client";

import { useEffect, type RefObject } from "react";

/** Further from the foot than this, a scroll up with no gesture seen behind it has let go. */
const LET_GO_PX = 150;
/** Close enough to the foot that the reader is following along again. */
const AT_FOOT_PX = 40;
/** Keys that move a focused transcript up - a move up, like a wheel's. */
const UP_KEYS = new Set(["ArrowUp", "PageUp", "Home"]);

/**
 * Whether the reader has moved up to read, written to `scrolledUp` for a
 * follower to ask before it pins the scroller to its foot.
 *
 * Decided by *intent*, not only by distance. While an answer streams, the
 * transcript grows every frame and is pinned back to its foot each time, so a
 * reader whose wheel moved it a few dozen pixels at a time never got as far as
 * a distance check waited for - every scroll up was undone before it counted,
 * and the chat could not be read while an agent was writing. Any move up - a
 * wheel, a finger dragging the page down, the keys, a grabbed scrollbar - lets
 * go at once; reaching the foot again picks the following back up.
 */
export function useReaderScrolledUp(
  scroller: RefObject<HTMLElement | null>,
  scrolledUp: { current: boolean },
): void {
  useEffect(() => {
    const element = scroller.current;
    if (!element) return;
    let touchY: number | null = null;
    let lastTop = element.scrollTop;
    let grabbing = false;
    const letGo = () => {
      scrolledUp.current = true;
    };
    // Far from the foot is a let-go only when the scroll went up. Content that
    // grows by more than a screen at once - a call's turns arriving together -
    // has the browser's scroll anchoring move the position *down* first, and
    // that scroll lands far from the new foot before the follower can pin it.
    // A wheel's scroll is animated and its first frames land a few pixels from
    // the foot - a scroll that moved up there is the reader leaving, so only one
    // that did not move up picks the following back up.
    const onScroll = () => {
      const top = element.scrollTop;
      const fromFoot = element.scrollHeight - top - element.clientHeight;
      if (fromFoot <= AT_FOOT_PX && top >= lastTop) scrolledUp.current = false;
      else if (top < lastTop && (grabbing || fromFoot > LET_GO_PX)) letGo();
      lastTop = top;
    };
    const onWheel = (event: WheelEvent) => {
      if (event.deltaY < 0) letGo();
    };
    const onTouchStart = (event: TouchEvent) => {
      touchY = event.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (event: TouchEvent) => {
      const y = event.touches[0]?.clientY;
      if (touchY !== null && y !== undefined && y > touchY) letGo();
    };
    const onKey = (event: KeyboardEvent) => {
      if (UP_KEYS.has(event.key)) letGo();
    };
    // A press on the scroller itself, not on anything inside it, is a press on
    // its scrollbar, and while it is held any scroll up lets go. The release is
    // heard on window, capturing: a drag ends wherever the pointer is let go,
    // usually outside the box. A browser that reports no such press keeps the
    // distance rule.
    const onPointerDown = (event: PointerEvent) => {
      grabbing = event.target === element;
    };
    const endGrab = () => {
      grabbing = false;
    };
    element.addEventListener("scroll", onScroll, { passive: true });
    element.addEventListener("wheel", onWheel, { passive: true });
    element.addEventListener("touchstart", onTouchStart, { passive: true });
    element.addEventListener("touchmove", onTouchMove, { passive: true });
    element.addEventListener("keydown", onKey);
    element.addEventListener("pointerdown", onPointerDown);
    window.addEventListener("pointerup", endGrab, true);
    window.addEventListener("pointercancel", endGrab, true);
    return () => {
      element.removeEventListener("scroll", onScroll);
      element.removeEventListener("wheel", onWheel);
      element.removeEventListener("touchstart", onTouchStart);
      element.removeEventListener("touchmove", onTouchMove);
      element.removeEventListener("keydown", onKey);
      element.removeEventListener("pointerdown", onPointerDown);
      window.removeEventListener("pointerup", endGrab, true);
      window.removeEventListener("pointercancel", endGrab, true);
    };
  }, [scroller, scrolledUp]);
}
