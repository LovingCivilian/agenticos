import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SyncSourceLogs } from "./sync-source-logs";
import { apiClient } from "@/lib/api-client";
import type { RAGSyncLog } from "@/lib/rag-api";

vi.mock("@/lib/api-client", () => ({
  apiClient: { get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() },
}));

function log(counts: Partial<RAGSyncLog>): RAGSyncLog {
  return {
    id: "log-1",
    source: "web",
    collection_name: "docs",
    mode: "new_only",
    status: "done",
    total_files: 0,
    ingested: 0,
    updated: 0,
    skipped: 0,
    failed: 0,
    removed: 0,
    error_message: null,
    started_at: "2026-09-22T10:00:00Z",
    completed_at: "2026-09-22T10:00:05Z",
    ...counts,
  };
}

async function shown(
  entry: RAGSyncLog,
  text: string | RegExp = /removed|ingested|no files processed/,
): Promise<HTMLElement> {
  vi.mocked(apiClient.get).mockResolvedValue({ items: [entry], total: 1 });
  render(<SyncSourceLogs logsPath="/kb/k/sync-sources/s/logs" />);
  fireEvent.click(screen.getByRole("button"));
  return screen.findByText(text);
}

beforeEach(() => {
  vi.mocked(apiClient.get).mockReset();
});

/**
 * The counts line says what the run did, and only that. A complete listing
 * that only removed documents used to read " · 3 removedno files processed":
 * a separator with nothing before it, glued to the empty state.
 */
describe("SyncSourceLogs counts", () => {
  it("shows a removal-only run as its removals alone", async () => {
    const line = await shown(log({ removed: 3 }));

    expect(line.textContent).toBe("3 removed");
  });

  it("joins the counts that happened, whichever comes first", async () => {
    const line = await shown(log({ total_files: 4, updated: 1, skipped: 2, removed: 1 }));

    expect(line.textContent).toBe("1 updated · 2 skipped · 1 removed");
  });

  it("says no files were processed only when nothing was counted", async () => {
    const line = await shown(log({}));

    expect(line.textContent).toBe("no files processed");
  });
});

/**
 * The message is red only for a failed run. A clean run can carry a note,
 * such as files that changed while it ran, and drawn in red it read as a
 * failure under a "done" status.
 */
describe("SyncSourceLogs message", () => {
  it("draws a failed run's message as an error", async () => {
    const failure = "1 files failed.";
    const message = await shown(
      log({ status: "error", failed: 1, error_message: failure }),
      failure,
    );

    expect(message).toHaveClass("text-destructive");
    expect(message).not.toHaveClass("text-muted-foreground");
  });

  it("draws a done run's note as muted text", async () => {
    const note =
      "2 files changed or were deleted while this sync ran, so they were left for the next sync.";
    const message = await shown(log({ skipped: 2, error_message: note }), note);

    expect(message).toHaveClass("text-muted-foreground");
    expect(message).not.toHaveClass("text-destructive");
  });
});
