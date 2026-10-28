// @vitest-environment jsdom

import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, WhatsAppIntegrationStatus } from "../api/client";
import WhatsAppIntegrationSettings from "./WhatsAppIntegrationSettings";

const status: WhatsAppIntegrationStatus = {
  provider: "whatsapp", connected: false, status: "disconnected", account_id: null, account_login: null,
  qr_data_url: null, sync_status: "idle", last_synced_at: null, oldest_message_at: null,
  retention_cutoff: null, error_type: null, remote_logout_failed: false,
};

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("WhatsApp integration settings", () => {
  it("explains transport relinking and keeps pairing available after setup failure", async () => {
    const refresh = vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue({
      ...status, status: "relink_required", account_id: "linked-id",
    });
    const view = render(<WhatsAppIntegrationSettings />);
    expect(await screen.findByText(/Whatsmeow bridge needs a new QR pairing/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Pair WhatsApp again" })).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Reconnect" })).toBeNull();
    view.unmount();
    refresh.mockResolvedValue({ ...status, status: "unavailable", error_type: "dependencies_missing" });
    render(<WhatsAppIntegrationSettings />);
    expect(await screen.findByRole("button", { name: "Pair WhatsApp" })).toBeTruthy();
  });

  it("keeps pairing QR data inside the detail page and polls pairing status", async () => {
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(status);
    vi.spyOn(api, "startWhatsAppPairing").mockResolvedValue({ ...status, status: "pairing" });
    vi.spyOn(api, "getWhatsAppPairingStatus").mockResolvedValue({ ...status, status: "pairing", qr_data_url: "data:image/png;base64,abc" });
    render(<WhatsAppIntegrationSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Pair WhatsApp" }));
    expect(await screen.findByRole("img", { name: "WhatsApp linking QR code" })).toBeTruthy();
    expect(api.getWhatsAppPairingStatus).toHaveBeenCalled();
    expect(screen.getByText(/history WhatsApp supplies/)).toBeTruthy();
  });

  it("requires confirmation before clearing cached messages", async () => {
    const connected = { ...status, connected: true, status: "connected", account_id: "linked-id", account_login: "My account" };
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(connected);
    vi.spyOn(api, "clearWhatsAppCache").mockResolvedValue(connected);
    render(<WhatsAppIntegrationSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Clear cached history" }));
    expect(screen.getByRole("dialog", { name: "Clear WhatsApp history?" })).toBeTruthy();
    expect(api.clearWhatsAppCache).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Clear history" }));
    await waitFor(() => expect(api.clearWhatsAppCache).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("The cached WhatsApp history was cleared.")).toBeTruthy();
  });

  it("reports when local disconnect needs a remote revoke follow-up", async () => {
    const connected = { ...status, connected: true, status: "connected", account_id: "linked-id" };
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(connected);
    vi.spyOn(api, "removeWhatsAppConnection").mockResolvedValue({ ...status, status: "disconnected", remote_logout_failed: true });
    render(<WhatsAppIntegrationSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect WhatsApp" }));
    fireEvent.click(screen.getByRole("button", { name: /^Disconnect$/ }));
    expect(await screen.findByText(/Remove Eidolon from Linked Devices/)).toBeTruthy();
  });

  it("allows pending QR pairing to be cancelled before an account id exists", async () => {
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue({ ...status, status: "pairing", qr_data_url: null });
    vi.spyOn(api, "removeWhatsAppConnection").mockResolvedValue(status);
    render(<WhatsAppIntegrationSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Disconnect WhatsApp" }));
    expect(screen.getByText(/cancels the pending WhatsApp pairing/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^Disconnect$/ }));
    await waitFor(() => expect(api.removeWhatsAppConnection).toHaveBeenCalledTimes(1));
  });

  it("offers retry cleanup and does not report success while local cleanup is pending", async () => {
    const pendingCleanup = { ...status, status: "unavailable", error_type: "cleanup_pending" };
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(pendingCleanup);
    vi.spyOn(api, "removeWhatsAppConnection").mockResolvedValue(pendingCleanup);
    render(<WhatsAppIntegrationSettings />);
    fireEvent.click(await screen.findByRole("button", { name: "Retry cleanup" }));
    fireEvent.click(within(screen.getByRole("dialog", { name: "Disconnect WhatsApp?" })).getByRole("button", { name: "Retry cleanup" }));
    expect(await screen.findByText(/cleanup is incomplete/i)).toBeTruthy();
    expect(screen.queryByText("WhatsApp was disconnected.")).toBeNull();
    expect(screen.getByRole("button", { name: "Retry cleanup" })).toBeTruthy();
  });

  it("requires confirmation and submits the account captured when Muse access was requested", async () => {
    const linked = { ...status, connected: true, status: "connected", account_id: "linked-id", account_login: "My WhatsApp account" };
    const changedAccount = { ...linked, account_id: "new-linked-id", account_login: "A different account" };
    const getStatus = vi.spyOn(api, "getWhatsAppConnection").mockResolvedValueOnce(linked).mockResolvedValueOnce(changedAccount);
    vi.spyOn(api, "setWhatsAppMusePermission").mockResolvedValue({ ...linked, muse_enabled: true, muse_grant_id: 9 });
    render(<WhatsAppIntegrationSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "Enable Assistant access" }));
    const dialog = screen.getByRole("dialog", { name: "Enable Assistant access to Muse?" });
    expect(within(dialog).getByText(/My WhatsApp account/)).toBeTruthy();
    expect(within(dialog).getByText("Muse")).toBeTruthy();
    expect(within(dialog).getByText(/shared with Meta and Muse/)).toBeTruthy();
    expect(within(dialog).getByText(/respond when Muse starts a conversation/)).toBeTruthy();
    expect(within(dialog).getByText(/roughly 30 messages across both sides/)).toBeTruthy();
    expect(api.setWhatsAppMusePermission).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(getStatus).toHaveBeenCalledTimes(2));
    expect(within(dialog).getByText(/My WhatsApp account/)).toBeTruthy();
    fireEvent.click(within(dialog).getByRole("button", { name: "Enable Assistant access" }));

    await waitFor(() => expect(api.setWhatsAppMusePermission).toHaveBeenCalledWith(true, "linked-id"));
    expect(await screen.findByText("Assistant access to Muse was enabled.")).toBeTruthy();
  });

  it("blocks enabling Muse access while WhatsApp is offline", async () => {
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(status);
    const setPermission = vi.spyOn(api, "setWhatsAppMusePermission");
    render(<WhatsAppIntegrationSettings />);

    const enable = await screen.findByRole("button", { name: "Enable Assistant access" });
    expect(enable).toHaveProperty("disabled", true);
    fireEvent.click(enable);
    expect(screen.queryByRole("dialog", { name: "Enable Assistant access to Muse?" })).toBeNull();
    expect(setPermission).not.toHaveBeenCalled();
  });

  it("allows Muse access to be revoked while WhatsApp is offline", async () => {
    const offlineGrant = { ...status, muse_enabled: true, muse_grant_id: 12 };
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(offlineGrant);
    vi.spyOn(api, "setWhatsAppMusePermission").mockResolvedValue({ ...offlineGrant, muse_enabled: false, muse_grant_id: null });
    render(<WhatsAppIntegrationSettings />);

    const disable = await screen.findByRole("button", { name: "Disable Assistant access" });
    expect(disable).toHaveProperty("disabled", false);
    fireEvent.click(disable);
    await waitFor(() => expect(api.setWhatsAppMusePermission).toHaveBeenCalledWith(false, null));
    expect(await screen.findByText("Assistant access to Muse was disabled.")).toBeTruthy();
  });

  it("shows Muse permission update errors and keeps the confirmation open for retry", async () => {
    const connected = { ...status, connected: true, status: "connected", account_id: "linked-id", account_login: "My account" };
    vi.spyOn(api, "getWhatsAppConnection").mockResolvedValue(connected);
    vi.spyOn(api, "setWhatsAppMusePermission").mockRejectedValue(new Error("The linked account changed"));
    render(<WhatsAppIntegrationSettings />);

    fireEvent.click(await screen.findByRole("button", { name: "Enable Assistant access" }));
    const dialog = screen.getByRole("dialog", { name: "Enable Assistant access to Muse?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Enable Assistant access" }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert.textContent).toContain("The linked account changed");
    expect(screen.getByRole("dialog", { name: "Enable Assistant access to Muse?" })).toBeTruthy();
    expect(api.getWhatsAppConnection).toHaveBeenCalledTimes(2);
  });
});
