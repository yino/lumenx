// @vitest-environment happy-dom

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({
  list: vi.fn(),
  get: vi.fn(),
  create: vi.fn(),
  complete: vi.fn(),
  cancel: vi.fn(),
  refund: vi.fn(),
  reconcile: vi.fn(),
  exportCsv: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  adminRechargeApi: {
    list: (...args: unknown[]) => mocks.list(...args),
    get: (...args: unknown[]) => mocks.get(...args),
    create: (...args: unknown[]) => mocks.create(...args),
    complete: (...args: unknown[]) => mocks.complete(...args),
    cancel: (...args: unknown[]) => mocks.cancel(...args),
    refund: (...args: unknown[]) => mocks.refund(...args),
    reconcile: (...args: unknown[]) => mocks.reconcile(...args),
  },
  adminPlatformApi: { exportCsv: (...args: unknown[]) => mocks.exportCsv(...args) },
  createIdempotencyKey: () => "recharge-create:test-key",
  getSafeApiError: (error: { code?: string; message?: string }) => ({
    code: error.code,
    message: error.message || "操作失败",
  }),
}));

import ManualRechargeAdminPage from "../ManualRechargeAdminPage";

const order = {
  id: "7",
  order_number: "MR202608160001",
  user_id: "42",
  cash_amount_fen: "1999",
  cash_amount_yuan: "19.99",
  ticket_amount_microtickets: "3123456",
  ticket_amount: "3.123456",
  currency: "CNY",
  status: "completed",
  status_zh: "已完成",
  exchange_snapshot: { tokens_per_ticket: 1000 },
  offline_reference: "OFFLINE-7",
  create_reason: "客服登记线下充值",
  refunded_cash_fen: "0",
  refunded_microtickets: "0",
  remaining_refundable_cash_fen: "1999",
  remaining_refundable_microtickets: "3123456",
  version: 2,
  created_by_admin_id: "1",
  completed_by_admin_id: "1",
  created_at: "2026-08-16T00:00:00Z",
  updated_at: "2026-08-16T00:01:00Z",
  completed_at: "2026-08-16T00:01:00Z",
  manual_confirmation: true,
} as const;

describe("人工充值订单后台", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mocks.list.mockResolvedValue({ items: [], total: 0, total_cash_fen: "0", total_ticket_microtickets: "0", offset: 0, limit: 30 });
    mocks.create.mockResolvedValue({ ...order, status: "pending", status_zh: "待确认", version: 1 });
    mocks.get.mockResolvedValue({
      order,
      wallet: { available_microtickets: "5000000", available_tickets: "5", held_microtickets: "2000000", held_tickets: "2" },
      events: [{ id: "11", event_type: "completed", event_type_zh: "人工确认到账", actor_admin_id: "1", cash_amount_fen: "1999", ticket_amount_microtickets: "3123456", ledger_entry_id: "21", version_before: 1, version_after: 2, reason: "财务确认线下到账", snapshot: {}, created_at: "2026-08-16T00:01:00Z" }],
      ledger: [{ id: "21", entry_type: "manual_recharge", amount_microtickets: "3123456", available_delta: "3123456", available_after: "5000000", held_after: "2000000", actor_admin_id: "1", reason: "财务确认线下到账", created_at: "2026-08-16T00:01:00Z" }],
    });
    mocks.reconcile.mockResolvedValue({ report_id: "31", order_id: "7", status: "mismatch", status_zh: "存在差异", severity: "high", details: { ledger_credit: "缺失" } });
  });

  it("以分和微算力券精确创建订单，并在提交中阻止重复操作", async () => {
    render(<ManualRechargeAdminPage />);
    await screen.findByText("没有符合条件的充值订单");
    fireEvent.click(screen.getByRole("button", { name: "新建订单" }));
    fireEvent.change(screen.getByLabelText("用户 ID"), { target: { value: "42" } });
    fireEvent.change(screen.getByLabelText("线下凭证号"), { target: { value: "OFFLINE-7" } });
    fireEvent.change(screen.getByLabelText("人民币金额（元）"), { target: { value: "19.99" } });
    fireEvent.change(screen.getByLabelText("充值算力券"), { target: { value: "3.123456" } });
    fireEvent.change(screen.getByLabelText("登记原因"), { target: { value: "客服登记线下充值" } });
    fireEvent.click(screen.getByRole("button", { name: "创建待确认订单" }));

    expect(screen.getByRole("button", { name: "创建待确认订单" })).toBeDisabled();
    await waitFor(() => expect(mocks.create).toHaveBeenCalledWith({
      user_id: 42,
      cash_amount_fen: 1999,
      ticket_amount_microtickets: 3_123_456,
      offline_reference: "OFFLINE-7",
      reason: "客服登记线下充值",
    }, "recharge-create:test-key"));
    expect(mocks.create).toHaveBeenCalledTimes(1);
  });

  it("展示事件和账本关联、退款余额，并明确对账不会修复历史", async () => {
    render(<ManualRechargeAdminPage orderId="7" />);
    expect(await screen.findByText("人工确认到账 · V1 → V2")).toBeInTheDocument();
    expect(screen.getByText("管理员 1 · 账本 #21")).toBeInTheDocument();
    expect(screen.getByText("余额 5 · 预扣 2")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "登记退款" }));
    expect(screen.getByText(/当前可用 5 券，预扣 2 券不可用于退款/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));

    fireEvent.click(screen.getByRole("button", { name: "运行对账" }));
    fireEvent.change(screen.getByLabelText("操作原因"), { target: { value: "财务核对订单和账本" } });
    fireEvent.click(screen.getByRole("button", { name: "生成报告" }));
    expect(await screen.findByText("存在差异")).toBeInTheDocument();
    expect(screen.getByText("对账仅生成报告，不会修改订单、钱包或账本历史。")).toBeInTheDocument();
  });
});
