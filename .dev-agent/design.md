# Design: chat follows again after a stale scroll-up

**One source file:** `frontend/src/components/chat/chat-container.tsx` (`ChatContainer`). The hooks stay as they are. `useReaderScrolledUp` sets `userScrolledUpRef` (line 478) on any move up and clears it only on a scroll within 40px of the foot. Both scroll-down paths check it: the `[messages, empty]` effect (544-554) and `holdFollow` (557).

**(a) A turn of the reader's own starts → follow it.** This matches assistant-ui (`runStarted`) and Open WebUI (scroll on the request going out, not on queueing). One effect, declared right after `useReaderScrolledUp(...)` (533), so it runs before the `[messages, empty]` effect in the same commit:
`useEffect(() => { if (isProcessing) userScrolledUpRef.current = false; }, [isProcessing]);`
`isProcessing` (from `useChat`, 355) turns true in only two places in `use-chat.ts`:
- `doSend` (1222). Every turn this tab starts goes through it: the composer (`send` → `sendMessage`), the welcome-screen picks, a visual's `onSend`, a held send flushing, `handleRegenerate`, the handed-in `prompt`, and the queue drainer (2075) sending a queued message.
- `turn_attached` (507): reopening or reloading a chat whose agent is still answering. Following it there is wanted too.

Not a turn start, so the reader stays where they are: queueing a message while a turn runs (`sendMessage` queues; `isProcessing` is already true), "send now" on a queued message (a steer into the running turn), a teammate's turn landing in a shared room, approvals resuming, and voice turns.

**(b) On a conversation change**, reset the flag in the existing effect (482-509), inside `if (shouldClear) {` (500), beside `clearQueued()` (505). That branch runs for id→null (new chat) and id→another id (a switch). Going from null to an id (`conversation_created` while the first answer streams) skips it, so a reader who scrolled up during that answer stays put. This matches assistant-ui's `scrollToBottomOnThreadSwitch` and Open WebUI's `autoScroll = true` on a new or loaded chat.

**After a reset**, nothing new runs. The `[messages, empty]` effect scrolls to the foot on the next messages change, and `holdFollow` returns false, so `useFollowContent` pins `scrollTop` to `scrollHeight` on each growth. An empty transcript is still held at its top by `emptyRef`.

**Tests:** new cases in `frontend/src/components/chat/chat-container.integration.test.tsx`, in `describe("the chat container - following the transcript")` (282). They mount the whole `ChatContainer` with the file's mocked `apiClient.get`, its `useWebSocket` mock and its real stores. `ResizeObserver` is faked through the block's `observeSizes()` helper; `scrollHeight`/`clientHeight` through `Object.defineProperty`. The reader's move is `fireEvent.wheel(scroller, { deltaY: -40 })`, a send is typing plus `fireEvent.submit(form)`, a switch is `useConversationStore.getState().setCurrentConversationId(...)`, and server frames come through the file's `receive(...)` helper. "Followed" means `scrollTop === scrollHeight` after the transcript's observer fires; jsdom's `scrollTo` is a no-op (`vitest.setup.ts:172`).

**CHANGELOG** (`[Unreleased]`, new `### Fixed`):
- **The chat follows a long answer again after a scroll up.** One scroll up, even on a new chat's empty welcome, stopped the transcript following every later answer until the scrollbar was dragged back to the bottom. Following now comes back when a turn of yours starts, including a queued message going out, and when you open or start a conversation.
