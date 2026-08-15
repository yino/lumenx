import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  getWallet: vi.fn(),
  getLedger: vi.fn(),
  getUsage: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  userTicketApi: mocks,
}));

import WalletPage from "../WalletPage";

describe("WalletPage", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.getWallet.mockResolvedValue({
      available_microtickets: "2500000",
      held_microtickets: "500000",
      total_microtickets: "3000000",
      available_tickets: "2.5",
      held_tickets: "0.5",
      total_tickets: "3",
      version: 1,
    });
    mocks.getLedger.mockResolvedValue({
      items: [{
        id: "ledger-1",
        entry_type: "grant",
        operation_zh: "算力券赠送",
        workspace_id: null,
        project_id: null,
        task_id: null,
        metering_tokens: null,
        amount_microtickets: "3000000",
        amount_tickets: "3",
        display_delta_microtickets: "3000000",
        display_delta_tickets: "3",
        available_after: "3000000",
        held_after: "0",
        available_after_tickets: "3",
        held_after_tickets: "0",
        status: "completed",
        status_zh: "已入账",
        reason: "注册赠送",
        created_at: "2026-08-13T08:00:00Z",
      }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    mocks.getUsage.mockResolvedValue({
      items: [{
        id: "usage-1",
        workspace_id: "workspace-1",
        project_id: "project-1",
        task_id: "task-1",
        capability: "image.t2i",
        outcome: "succeeded",
        status_zh: "已结算",
        metering_tokens: "1000",
        charged_microtickets: "1000000",
        charged_tickets: "1",
        tokens_per_ticket: "1000",
        created_at: "2026-08-13T09:00:00Z",
      }],
      total: 1,
      offset: 0,
      limit: 20,
    });
  });

  it("显示可用、预扣与账务流水", async () => {
    render(<WalletPage />);

    expect(await screen.findByText("2.5")).toBeInTheDocument();
    expect(screen.getByText("0.5")).toBeInTheDocument();
    expect(screen.getByText("算力券赠送")).toBeInTheDocument();
    expect(screen.getByText("注册赠送")).toBeInTheDocument();
  });

  it("切换到用量明细后显示计量令牌和算力券消耗", async () => {
    render(<WalletPage />);
    await screen.findByText("算力券赠送");

    fireEvent.click(screen.getByRole("button", { name: "用量明细" }));

    await waitFor(() => expect(mocks.getUsage).toHaveBeenCalledWith(0, 20));
    expect(await screen.findByText("文生图")).toBeInTheDocument();
    expect(screen.getByText("计量令牌 1000")).toBeInTheDocument();
    expect(screen.getByText("-1")).toBeInTheDocument();
  });
});
