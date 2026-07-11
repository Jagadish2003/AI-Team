/**
 * R18-A3 T5 (AT-558) — NetworkProfileContext.
 *
 * Loads the deployment network profile + per-connector auth capability once
 * (GET /api/network-profile) and exposes it to the Integration Hub so tiles and
 * the connector detail panel can gate connect flows consistently.
 *
 * Fail-open: if the fetch fails or has not resolved yet, the profile defaults to
 * `standard` with no capabilities, so the UI never wrongly hides a Connect flow.
 * The backend remains the source of truth — this only drives what the hub offers.
 */
import React, {
  createContext,
  useCallback,
  useContext,
  useMemo,
} from 'react';
import { authHeaderForToken } from '../lib/apiClient';
import type {
  ConnectorAuthCapability,
  NetworkProfileResponse,
} from '../types/networkProfile';
import { useAuthOptional } from './AuthContext';
import { useResource } from '../lib/dataCache';
import { cacheKeys } from '../lib/cacheKeys';

interface NetworkProfileContextValue {
  /** True when the deployment exposes no public inbound HTTPS. */
  noPublicInbound: boolean;
  /** Per-connector auth capability, keyed by connector id. */
  capabilities: Record<string, ConnectorAuthCapability>;
  /** Capability for one connector, or null when unknown/not yet loaded. */
  capabilityFor: (connectorId: string) => ConnectorAuthCapability | null;
  /**
   * True when the browser authorization-code Connect flow must be HIDDEN for this
   * connector: the deployment is no-public-inbound AND the connector has an
   * outbound-only mode to route the customer to instead (AC4). Connectors with no
   * outbound-only mode (GitHub, Slack) keep their Connect button — they fall back
   * to the scoped-inbound package (R18-A3 T6).
   */
  hidesAuthorizationCodeConnect: (connectorId: string) => boolean;
  loading: boolean;
}

const Ctx = createContext<NetworkProfileContextValue | null>(null);

export function NetworkProfileProvider({ children }: { children: React.ReactNode }) {
  const auth = useAuthOptional();
  const token = auth?.token ?? null;

  // Backed by the shared cache so connector mutations that invalidate
  // cacheKeys.networkProfile (connect/disconnect can change auth-capability
  // gating) refresh this live. Fail-open: a fetch failure resolves to null →
  // standard profile, no gating. Raw fetch (not apiGet) with the token passed
  // explicitly — same rationale as ConnectorContext (dodges the global 401
  // handler firing from this always-mounted chrome pre-login, and the
  // first-mount token race).
  const fetchProfile = useCallback(async (): Promise<NetworkProfileResponse | null> => {
    const base =
      (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
      'http://localhost:8000';
    const res = await fetch(`${base}/api/network-profile`, {
      headers: authHeaderForToken(token),
    });
    return res.ok ? ((await res.json()) as NetworkProfileResponse) : null;
  }, [token]);

  const resource = useResource<NetworkProfileResponse | null>(
    cacheKeys.networkProfile,
    fetchProfile,
  );
  // Fail-open: a rejected fetch (resource.error) resolves to null → standard.
  const data = resource.error ? null : resource.data ?? null;
  // True only before the first load — background refetches (from a connector
  // mutation invalidating 'network-profile') keep the current data and must not
  // flip loading, so consumers never flash/remount on a refresh.
  const loading = resource.data === undefined && resource.error === null;

  const value = useMemo<NetworkProfileContextValue>(() => {
    const capabilities = data?.connectors ?? {};
    const noPublicInbound = data?.no_public_inbound ?? false;
    const capabilityFor = (connectorId: string) =>
      capabilities[connectorId] ?? null;
    const hidesAuthorizationCodeConnect = (connectorId: string) => {
      if (!noPublicInbound) return false;
      const cap = capabilities[connectorId];
      return Boolean(cap?.has_outbound_only_mode);
    };
    return {
      noPublicInbound,
      capabilities,
      capabilityFor,
      hidesAuthorizationCodeConnect,
      loading,
    };
  }, [data, loading]);

  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useNetworkProfile(): NetworkProfileContextValue {
  const ctx = useContext(Ctx);
  if (!ctx) {
    throw new Error('useNetworkProfile must be used inside NetworkProfileProvider');
  }
  return ctx;
}

/**
 * Non-throwing variant for components that may render outside the provider (e.g.
 * in isolated unit tests). Returns a safe standard-profile default when absent.
 */
export function useNetworkProfileOptional(): NetworkProfileContextValue {
  const ctx = useContext(Ctx);
  if (ctx) return ctx;
  return {
    noPublicInbound: false,
    capabilities: {},
    capabilityFor: () => null,
    hidesAuthorizationCodeConnect: () => false,
    loading: false,
  };
}
