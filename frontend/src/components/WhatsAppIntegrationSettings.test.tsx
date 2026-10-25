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
});
