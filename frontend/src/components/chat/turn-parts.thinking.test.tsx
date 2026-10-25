import { fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ThinkingBlock } from "./turn-parts";

const TEXT = "Weighing the options carefully. Then deciding.\n\nA second paragraph follows here.";

/** Captures the follower's observer so a test can say when the content grew. */
let grew: (() => void) | undefined;

beforeEach(() => {
  grew = undefined;
  class FakeResize {
    constructor(callback: () => void) {
      grew = callback;
    }
    observe() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", FakeResize);
});

afterEach(() => vi.unstubAllGlobals());

function mount(isStreaming: boolean) {
  const { container } = render(<ThinkingBlock text={TEXT} open isStreaming={isStreaming} />);
  const scroller = container.querySelector<HTMLElement>(".overflow-y-auto")!;
  return { container, scroller };
}

/** The geometry jsdom never computes: the content is now this tall. */
function grow(scroller: HTMLElement, height = 900) {
  if (!grew) throw new Error("nothing observes the thinking box");
  Object.defineProperty(scroller, "scrollHeight", { configurable: true, value: height });
  grew();
}

describe("ThinkingBlock streaming effects", () => {
  it("fades words in and lets blocks rise while streaming", async () => {
    const { container, scroller } = mount(true);

    await waitFor(() => expect(container.querySelector(".stream-word")).not.toBeNull());
    // `.stream-in > *` animates direct children only, so the blocks sit right under it.
    expect(scroller.querySelector(".stream-in > p")).not.toBeNull();
  });

  it("looks as it always did when finished", async () => {
    const { container, scroller } = mount(false);

    await waitFor(() => expect(container.querySelector("p")).not.toBeNull());
    expect(container.querySelector(".stream-word")).toBeNull();
    expect(container.querySelector(".stream-in")).toBeNull();
    expect(scroller).toHaveClass("max-h-72", "overflow-y-auto");
  });

  it("lets the last words finish fading when the stream ends, then drops the effects", async () => {
    const { container, rerender } = render(<ThinkingBlock text={TEXT} open isStreaming />);
    await waitFor(() => expect(container.querySelector(".stream-word")).not.toBeNull());

    rerender(<ThinkingBlock text={TEXT} open isStreaming={false} />);

    expect(container.querySelector(".stream-in")).toBeNull();
    expect(container.querySelector(".stream-word")).not.toBeNull();
    await waitFor(() => expect(container.querySelector(".stream-word")).toBeNull());
  });

  it("follows the content to its bottom while streaming", () => {
    const { scroller } = mount(true);

    grow(scroller);

    expect(scroller.scrollTop).toBe(900);
  });

  it("leaves a reader who scrolled up inside it where they are", () => {
    const { scroller } = mount(true);

    grow(scroller);
    expect(scroller.scrollTop).toBe(900);

    fireEvent.wheel(scroller, { deltaY: -40 });
    grow(scroller, 1500);

    expect(scroller.scrollTop).toBe(900);
  });

  it("does not jump a finished reasoning to its end", () => {
    const { scroller } = mount(false);

    grow(scroller);

    expect(scroller.scrollTop).toBe(0);
  });
});
