# Implementation plan

Tests come first (TDD). Cases 1, 2 and 4 must fail before the edits; 3 and 5 are guards that pass before and after.

**Edits in `frontend/src/components/chat/chat-container.tsx`, in this order:**

1. Right after `useReaderScrolledUp(scrollContainerRef, userScrolledUpRef);` (533), before the `[messages, empty]` effect:
   ```
   // A turn of the reader's own starting - a send, a regenerate, a queued message
   // going out - is followed from its first line: a scroll up before it, even on
   // the welcome where nothing scrolls, would leave its answer growing below the
   // fold. Queueing one while a turn runs is not a start, so it moves nobody.
   useEffect(() => {
     if (isProcessing) userScrolledUpRef.current = false;
   }, [isProcessing]);
   ```
   It must be declared before the `[messages, empty]` effect, so in the commit that adds the reader's message the reset runs first and that effect scrolls to the foot.
2. In the conversation-change effect, inside `if (shouldClear)` after `clearQueued()` (505):
   ```
   // Another thread, or a new one, is read from its end: a scroll up was about
   // the one left. Not when a new chat is named mid-answer - that reader stays.
   userScrolledUpRef.current = false;
   ```
3. In `CHANGELOG.md`, under `## [Unreleased]`, add `### Fixed` with the line from `design.md`.

**Test cases in `chat-container.integration.test.tsx`, in the "following the transcript" block.** Add a helper `growTranscript(observers, scroller, height)` that sets `scrollHeight` and calls `grew()` on every observer watching `scroller.firstElementChild` (`transcriptRef`). Await the reader's message on screen before growing: an empty transcript also holds the follow (`emptyRef`).

1. **Welcome, wheel up, send → followed.** Mount on the welcome, wheel up, send a question, await it, set `scrollTop = 0`, grow to 2600. Expect 2600.
2. **Wheel up, switch → followed.** Open `"c-a"`, add a message, wheel up, switch to `"c-b"` in `act`, await c-b's message, set `scrollTop = 0`, grow. Expect the new height.
3. **Guard: a scroll up mid-answer is not followed.** Mid-turn (`receive("model_request_start")`), set `scrollTop = 1500`, wheel up, set `scrollTop = 200`, grow. Expect 200.
4. **Queued follow-up: not moved on queueing, followed when it goes out.** Send, start the turn, wheel up, send a second message (it queues). Grow → `scrollTop` unchanged. End the turn (the file's completion frame) so the drainer sends the queued message; await it; grow → followed.
5. **Guard (optional): a new chat named mid-answer keeps the hold.** Send from a new chat, start the turn, wheel up, `receive("conversation_created", { conversation_id: "c-new" })`, grow → not followed.

**Ambiguities:**
- If the file's mocks make the drainer hard to drive in case 4, it may be split: queueing does not move the reader (asserted), and `isProcessing` turning on resets the flag (case 1 already proves it). Note it rather than skip it.
- Regenerate and the clicked bubble go through `doSend` like a send, so they need no test of their own.

**Verify:** from `frontend/`, `bunx vitest run src/components/chat/chat-container.integration.test.tsx`; from the root, `make lint-frontend`.
