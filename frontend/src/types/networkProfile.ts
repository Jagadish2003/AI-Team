/**
 * R18-A3 T5 (AT-558) — NETWORK_PROFILE deployment flag + per-connector auth
 * capability, as returned by GET /api/network-profile.
 *
 * Mirrors the backend `NetworkProfileResponse` in
 * `backend/app/routes_connector_auth.py`. Fields are snake_case to match the raw
 * JSON the backend emits (no transform layer).
 *
 * The UI pairs `no_public_inbound` with each connector's `has_outbound_only_mode`
 * to decide, per tile, whether to offer the browser authorization-code Connect
 * flow or route the customer to the outbound setup path. In a
 * `no_public_inbound` deployment the authorization-code Connect button is hidden
 * exactly where an outbound-only mode exists, so the customer can never start a
 * flow that cannot complete (AC4).
 */

export type NetworkProfileValue = 'standard' | 'no_public_inbound';

export type AuthModeValue =
  | 'authorization_code'
  | 'client_credentials'
  | 'jwt_bearer'
  | 'static';

export interface ConnectorAuthCapability {
  connector_id: string;
  /** All modes the connector supports, most-preferred first. */
  supported_auth_modes: AuthModeValue[];
  /** The subset that needs NO inbound callback (client_credentials/jwt_bearer/static). */
  outbound_only_modes: AuthModeValue[];
  /** True when at least one outbound-only mode exists — the AC4 gating flag. */
  has_outbound_only_mode: boolean;
  /** Default mode (first supported); null for an unknown connector. */
  default_auth_mode: AuthModeValue | null;
}

export interface NetworkProfileResponse {
  network_profile: NetworkProfileValue;
  no_public_inbound: boolean;
  connectors: Record<string, ConnectorAuthCapability>;
}
