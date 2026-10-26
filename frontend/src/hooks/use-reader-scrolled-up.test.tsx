import { fireEvent, render, screen } from "@testing-library/react";
import { useRef } from "react";
import { describe, expect, it } from "vitest";

import { useReaderScrolledUp } from "./use-reader-scrolled-up";

const state = { current: false };

function Scroller({ attach = true }: { attach?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  useReaderScrolledUp(ref, state);
  return attach ? (
    <div ref={ref} data-testid="scroller">
      <span data-testid="content" />
    </div>
  ) : null;
}

/** The geometry jsdom never computes, as a browser would report it. */
function at(element: HTMLElement, fromFoot: number, height = 2000) {
  Object.defineProperty(element, "scrollHeight", { configurable: true, value: height });
  Object.defineProperty(element, "clientHeight", { configurable: true, value: 500 });
  Object.defineProperty(element, "scrollTop", {
    configurable: true,
    value: height - 500 - fromFoot,
  });
  fireEvent.scroll(element);
}

function mount() {
  state.current = false;
  render(<Scroller />);
  return screen.getByTestId("scroller");
}

/**
 * A reader scrolling up while an answer streams is left where they went - any
 * move up lets go, however small - and following resumes at the foot.
 */
describe("useReaderScrolledUp", () => {
  it("lets go on a wheel turned up, however little, and not on one turned down", () => {
    const scroller = mount();

    fireEvent.wheel(scroller, { deltaY: 40 });
    expect(state.current).toBe(false);
    fireEvent.wheel(scroller, { deltaY: -20 });
    expect(state.current).toBe(true);
  });

  it("lets go on a finger dragging the page down, and not up", () => {
    const scroller = mount();

    fireEvent.touchStart(scroller, { touches: [{ clientY: 300 }] });
    fireEvent.touchMove(scroller, { touches: [{ clientY: 250 }] });
    expect(state.current).toBe(false);
    fireEvent.touchMove(scroller, { touches: [{ clientY: 340 }] });
    expect(state.current).toBe(true);
  });

  it("lets go on the keys that move up", () => {
    const scroller = mount();

    fireEvent.keyDown(scroller, { key: "End" });
    expect(state.current).toBe(false);
    fireEvent.keyDown(scroller, { key: "PageUp" });
    expect(state.current).toBe(true);
  });

  it("follows again at the foot, and lets go of a scrollbar dragged far up", () => {
    const scroller = mount();

    at(scroller, 0);
    at(scroller, 600);
    expect(state.current).toBe(true);
    at(scroller, 100);
    expect(state.current).toBe(true);
    at(scroller, 10);
    expect(state.current).toBe(false);
  });

  it("keeps following when content grows by more than a screen at once", () => {
    // Scroll anchoring moves the position down with the growth, and that scroll
    // lands far from the new foot before the follower pins it.
    const scroller = mount();

    at(scroller, 0);
    at(scroller, 512, 3000);

    expect(state.current).toBe(false);
  });

  it("stays let go when a wheel's first scroll frames land near the foot, still moving up", () => {
    const scroller = mount();

    at(scroller, 0);
    fireEvent.wheel(scroller, { deltaY: -20 });
    at(scroller, 12);

    expect(state.current).toBe(true);
  });

  it("follows again on a scroll down to the foot, and on a scroll that did not move there", () => {
    const scroller = mount();

    at(scroller, 0);
    fireEvent.wheel(scroller, { deltaY: -20 });
    at(scroller, 12);
    at(scroller, 5);
    expect(state.current).toBe(false);

    fireEvent.wheel(scroller, { deltaY: -20 });
    expect(state.current).toBe(true);
    fireEvent.scroll(scroller);
    expect(state.current).toBe(false);
  });

  it("lets go of a small scroll up while the scrollbar is held, and not once it is released outside", () => {
    const scroller = mount();

    at(scroller, 0);
    fireEvent.pointerDown(scroller);
    at(scroller, 60);
    expect(state.current).toBe(true);

    fireEvent.pointerUp(document.body);
    at(scroller, 0);
    expect(state.current).toBe(false);
    at(scroller, 60);
    expect(state.current).toBe(false);
  });

  it("ends a grab that the browser cancels", () => {
    const scroller = mount();

    at(scroller, 0);
    fireEvent.pointerDown(scroller);
    at(scroller, 60);
    expect(state.current).toBe(true);

    fireEvent.pointerCancel(document.body);
    at(scroller, 0);
    expect(state.current).toBe(false);
    at(scroller, 60);
    expect(state.current).toBe(false);
  });

  it("does not take a press on the content for a grab", () => {
    const scroller = mount();

    at(scroller, 0);
    fireEvent.pointerDown(screen.getByTestId("content"));
    at(scroller, 60);

    expect(state.current).toBe(false);
  });

  it("keeps the distance rule for a small scroll up with no gesture behind it", () => {
    const scroller = mount();

    at(scroller, 0);
    at(scroller, 60);
    expect(state.current).toBe(false);
    at(scroller, 140);
    expect(state.current).toBe(false);
    at(scroller, 200);
    expect(state.current).toBe(true);
  });

  it("ignores a move with no touch it started from", () => {
    const scroller = mount();

    fireEvent.touchMove(scroller, { touches: [{ clientY: 340 }] });

    expect(state.current).toBe(false);
  });

  it("does nothing without a scroller, and stops listening once it is gone", () => {
    state.current = false;
    render(<Scroller attach={false} />).unmount();
    const { unmount } = render(<Scroller />);
    unmount();

    expect(state.current).toBe(false);
  });
});
