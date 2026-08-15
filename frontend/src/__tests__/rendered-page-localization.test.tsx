// @vitest-environment happy-dom

import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  auditRenderedChineseCopy,
  formatVisibleCopyViolations,
  type VisibleCopyAuditOptions,
} from "@/lib/visibleCopyPolicy";

const mocks = vi.hoisted(() => ({
  login: vi.fn(),
  register: vi.fn(),
  registrationPolicy: vi.fn(),
  getWallet: vi.fn(),
  getLedger: vi.fn(),
  getUsage: vi.fn(),
  listUsers: vi.fn(),
  listTasks: vi.fn(),
  listUsage: vi.fn(),
  listAuditEvents: vi.fn(),
  listImportBatches: vi.fn(),
  listInvitations: vi.fn(),
  listVersions: vi.fn(),
  getActiveConfiguration: vi.fn(),
  getDeploymentState: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  authApi: {
    login: mocks.login,
    register: mocks.register,
    registrationPolicy: mocks.registrationPolicy,
  },
  userTicketApi: {
    getWallet: mocks.getWallet,
    getLedger: mocks.getLedger,
    getUsage: mocks.getUsage,
  },
  adminPlatformApi: {
    listUsers: mocks.listUsers,
    listTasks: mocks.listTasks,
    listUsage: mocks.listUsage,
    listAuditEvents: mocks.listAuditEvents,
    listImportBatches: mocks.listImportBatches,
    listInvitations: mocks.listInvitations,
  },
  adminConfigurationApi: {
    listVersions: mocks.listVersions,
    getActive: mocks.getActiveConfiguration,
    getDeploymentState: mocks.getDeploymentState,
  },
  adminTicketApi: {},
  getSafeApiError: (error: { message?: string }) => ({
    message: error.message || "操作失败，请稍后重试",
  }),
  getSafeAuthError: (error: { message?: string }) => ({
    message: error.message || "登录失败，请稍后重试",
  }),
}));

import PlatformAdminPage from "@/components/admin/PlatformAdminPage";
import AuthScreen from "@/components/auth/AuthScreen";
import WalletPage from "@/components/wallet/WalletPage";
import WorkspaceSwitcher from "@/components/workspace/WorkspaceSwitcher";
import { useWorkspaceStore } from "@/store/workspaceStore";

function expectChineseRenderedCopy(
  root: ParentNode,
  options?: VisibleCopyAuditOptions,
): void {
  const violations = auditRenderedChineseCopy(root, options);
  expect(formatVisibleCopyViolations(violations)).toBe("");
}

describe("主要页面渲染文案本地化", () => {
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
    mocks.registrationPolicy.mockResolvedValue({
      mode: "invite_only",
      verification_available: false,
    });
    mocks.getLedger.mockResolvedValue({
      items: [{
        id: "ledger-1",
        entry_type: "grant",
        operation_zh: "算力券赠送",
        workspace_id: "workspace-1",
        project_id: "project-1",
        task_id: null,
        metering_tokens: "1000",
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
        reason: "新用户赠送",
        created_at: "2026-08-13T08:00:00Z",
      }],
      total: 1,
      offset: 0,
      limit: 20,
    });
    mocks.getUsage.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 20 });
    mocks.listUsers.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 30 });
    mocks.listTasks.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 30 });
    mocks.listUsage.mockResolvedValue({
      items: [],
      total: 0,
      offset: 0,
      limit: 30,
      total_metering_tokens: "0",
      total_charged_tickets: "0",
    });
    mocks.listAuditEvents.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 30 });
    mocks.listImportBatches.mockResolvedValue({ items: [], total: 0, offset: 0, limit: 30 });
    mocks.listInvitations.mockResolvedValue([]);
    mocks.listVersions.mockResolvedValue([]);
    mocks.getActiveConfiguration.mockResolvedValue(null);
    mocks.getDeploymentState.mockResolvedValue({
      worker_concurrency: 8,
      authority: "environment_or_compose",
      activation_required: true,
      deployment_mode: "cloud",
      object_store_adapter: "oss",
      provider_adapter: "production",
      test_adapters_enabled: false,
      oss_private: true,
      registration_emergency_disabled: true,
      new_ai_tasks_emergency_disabled: true,
      resource_fingerprints: {
        postgresql: "sha256:1111111111111111111111111111111111111111111111111111111111111111",
        redis: "sha256:2222222222222222222222222222222222222222222222222222222222222222",
        oss_bucket: "sha256:3333333333333333333333333333333333333333333333333333333333333333",
        provider_account: "sha256:4444444444444444444444444444444444444444444444444444444444444444",
      },
    });

    useWorkspaceStore.setState({
      status: "ready",
      workspaces: [{
        id: "workspace-1",
        name: "默认工作区",
        version: 1,
        deleted: false,
        retention_expires_at: null,
      }],
      deletedWorkspaces: [],
      currentWorkspaceId: "workspace-1",
      ownerUserId: "user-1",
      busyWorkspaceId: null,
      error: null,
    });
  });

  it("登录和注册入口没有未经批准的英文产品文案", () => {
    const { container } = render(
      <AuthScreen initialMode="register" onAuthenticated={vi.fn()} />,
    );

    expectChineseRenderedCopy(container, { technicalValues: ["LUMEN", "X"] });
  });

  it("工作区入口没有未经批准的英文产品文案", () => {
    const { container } = render(<WorkspaceSwitcher />);
    fireEvent.click(screen.getByRole("button", { expanded: false }));

    expectChineseRenderedCopy(container);
  });

  it("算力券页面没有未经批准的英文产品文案", async () => {
    const { container } = render(<WalletPage />);
    await screen.findByText("算力券赠送");

    expectChineseRenderedCopy(container);
  });

  for (const [section, emptyCopy] of [
    ["users", "没有符合条件的用户"],
    ["invitations", "暂无注册邀请"],
    ["tasks", "暂无 AI 任务"],
    ["usage", "暂无用量记录"],
    ["tickets", "查询钱包"],
    ["configuration", "暂无配置版本"],
    ["audit-events", "暂无审计事件"],
    ["import-batches", "暂无导入批次"],
  ] as const) {
    it(`平台管理${section}分区没有未经批准的英文产品文案`, async () => {
      const { container } = render(<PlatformAdminPage section={section} />);
      await screen.findByText(emptyCopy);

      expectChineseRenderedCopy(container);
    });
  }
});
