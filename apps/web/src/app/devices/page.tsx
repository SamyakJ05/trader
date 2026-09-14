"use client";

import Shell from "@/components/Shell";
import { Button, Card, EmptyState, PageHeader, Pill, Skeleton, Td, Th } from "@/components/ui";
import { useToast } from "@/components/toast";
import { api } from "@/lib/api";
import { useApi } from "@/lib/useApi";
import { DeviceSession } from "@/lib/types";

/** Trims the noise out of a user-agent so the table stays scannable. */
function describe(userAgent: string | null): string {
  if (!userAgent) return "Unknown device";
  const browser =
    /Edg\//.test(userAgent) ? "Edge"
    : /Chrome\//.test(userAgent) ? "Chrome"
    : /Safari\//.test(userAgent) ? "Safari"
    : /Firefox\//.test(userAgent) ? "Firefox"
    : "Browser";
  const os =
    /iPhone|iPad/.test(userAgent) ? "iOS"
    : /Android/.test(userAgent) ? "Android"
    : /Mac OS X/.test(userAgent) ? "macOS"
    : /Windows/.test(userAgent) ? "Windows"
    : /Linux/.test(userAgent) ? "Linux"
    : "";
  return os ? `${browser} on ${os}` : browser;
}

export default function DevicesPage() {
  const { data: sessions, loading, reload } = useApi<DeviceSession[]>("/auth/sessions");
  const { push } = useToast();

  async function revoke(session: DeviceSession) {
    try {
      await api(`/auth/sessions/${session.id}`, { method: "DELETE" });
      if (session.current) {
        // Revoking the current session signs this browser out.
        window.location.href = "/login";
        return;
      }
      push("success", "Device signed out");
      reload();
    } catch (err) {
      push("error", err instanceof Error ? err.message : "Failed");
    }
  }

  return (
    <Shell>
      <PageHeader
        title="Devices"
        sub="Where your account is signed in. Sign out anything you don't recognise."
      />

      {loading && !sessions ? (
        <Skeleton className="h-40" />
      ) : !sessions?.length ? (
        <EmptyState title="No active sessions" />
      ) : (
        <Card title={`Active sessions (${sessions.length})`} pad={false}>
          <table className="w-full">
            <thead>
              <tr>
                <Th>Device</Th>
                <Th>IP</Th>
                <Th>Signed in</Th>
                <Th>Last seen</Th>
                <Th right>Actions</Th>
              </tr>
            </thead>
            <tbody>
              {sessions.map((s) => (
                <tr key={s.id}>
                  <Td>
                    {describe(s.user_agent)}
                    {s.current && (
                      <span className="ml-2">
                        <Pill value="connected" label="THIS DEVICE" />
                      </span>
                    )}
                  </Td>
                  <Td>{s.ip ?? "—"}</Td>
                  <Td>{new Date(s.created_at).toLocaleString()}</Td>
                  <Td>{s.last_seen_at ? new Date(s.last_seen_at).toLocaleString() : "—"}</Td>
                  <Td right>
                    <Button variant={s.current ? undefined : "danger"} onClick={() => revoke(s)}>
                      {s.current ? "Sign out" : "Revoke"}
                    </Button>
                  </Td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </Shell>
  );
}
