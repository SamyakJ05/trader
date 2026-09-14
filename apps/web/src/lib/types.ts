export interface User {
  id: string;
  email: string;
  full_name: string | null;
  is_admin: boolean;
  totp_enabled?: boolean;
}

export interface AdminUser extends User {
  is_active: boolean;
  created_at: string;
  active_sessions: number;
  totp_enabled: boolean;
}

export interface Invite {
  id: string;
  email: string;
  full_name: string | null;
  as_admin: boolean;
  created_at: string;
  expires_at: string;
  consumed_at: string | null;
  revoked_at: string | null;
  /** Present only when the invite email failed to send. */
  invite_url?: string | null;
}

export interface DeviceSession {
  id: string;
  user_agent: string | null;
  ip: string | null;
  created_at: string;
  last_seen_at: string | null;
  current: boolean;
}

export interface CredentialStatus {
  env_keys_configured: boolean;
  env_access_token_configured: boolean;
  session_token_configured: boolean;
  session_expires_at: string | null;
}

export interface BrokerAccount {
  id: string;
  broker: string;
  label: string;
  environment: "paper" | "live";
  status: string;
  status_message: string | null;
  live_enabled: boolean;
  last_sync_at: string | null;
  read_verified_at: string | null;
  credential_ref: string | null;
  broker_client_id: string | null;
  adapter_status: "working" | "scaffold" | "planned";
  credentials: CredentialStatus;
}

export interface DashboardSummary {
  accounts: {
    total: number;
    connected: number;
    by_broker: Record<string, number>;
    by_environment: Record<string, number>;
    live_configured: number;
    never_synced: string[];
    read_unverified: string[];
  };
  per_account: {
    id: string;
    broker: string;
    label: string;
    environment: string;
    status: string;
    last_sync_at: string | null;
    read_verified_at: string | null;
    available_cash: string | null;
    funds_as_of: string | null;
    holdings_count: number | null;
  }[];
  funds: { total_available_cash: string; complete: boolean };
  open_positions: number;
  open_orders: number;
  strategies: Record<string, number>;
  killswitch: KillSwitchStatus;
  recent_events: {
    id: number;
    ts: string;
    event_type: string;
    entity_type: string | null;
    entity_id: string | null;
    payload: Record<string, unknown>;
  }[];
}

export interface BrokerCapabilities {
  broker: string;
  adapter_status: "working" | "scaffold" | "planned";
  display_name: string;
  auth_model: string;
  session_validity: string;
  rate_limit_notes: string;
  place_order: boolean;
  modify_order: boolean;
  cancel_order: boolean;
  holdings: boolean;
  positions: boolean;
  funds: boolean;
  instruments_dump: boolean;
  websocket_ticks: boolean;
  order_postbacks: boolean;
  amo_orders: boolean;
  bracket_gtt: boolean;
  exchanges: string[];
  notes: string;
}

export interface Order {
  id: string;
  client_order_id: string;
  broker_order_id: string | null;
  environment: string;
  symbol: string;
  exchange: string;
  side: string;
  order_type: string;
  product: string;
  quantity: number;
  filled_quantity: number;
  price: string | null;
  trigger_price: string | null;
  average_fill_price: string | null;
  status: string;
  status_message: string | null;
  strategy_id: string | null;
  placed_at: string;
  updated_at: string;
}

export interface Position {
  id: string;
  broker_account_id: string;
  symbol: string;
  exchange: string;
  product: string;
  quantity: number;
  average_price: string;
  last_price: string;
  realized_pnl: string;
  unrealized_pnl: string;
}

export interface Strategy {
  id: string;
  name: string;
  kind: string;
  environment: string;
  symbols: string[];
  params: Record<string, unknown>;
  status: string;
  broker_account_id: string | null;
  created_at: string;
  killed: boolean;
}

export interface RiskRule {
  id: string;
  rule_type: string;
  environment: string;
  params: Record<string, unknown>;
  enabled: boolean;
}

export interface AuditEvent {
  id: number;
  ts: string;
  event_type: string;
  entity_type: string | null;
  entity_id: string | null;
  correlation_id: string | null;
  payload: Record<string, unknown>;
}

export interface KillSwitchStatus {
  global_engaged: boolean;
  global_reason: string | null;
  killed_strategies: string[];
}

export interface AIStatus {
  configured: boolean;
  provider: string | null;
  model: string | null;
}

export interface AISettingsInfo {
  provider: string | null;
  model: string | null;
  base_url: string | null;
  configured: boolean;
  source: "env" | "settings" | null;
  providers: string[];
  default_models: Record<string, string>;
}

export interface AIProposal {
  id: string;
  broker_account_id: string;
  symbol: string;
  exchange: string;
  side: string;
  order_type: string;
  product: string;
  quantity: number;
  limit_price: string | null;
  rationale: string;
  status: string;
  order_id: string | null;
  created_at: string;
  decided_at: string | null;
}

export interface ChatTurn {
  role: "user" | "assistant";
  content: string;
  proposals?: AIProposal[];
}

export interface StrategyDraft {
  name: string;
  kind: string;
  symbols: string[];
  params: Record<string, unknown>;
  rationale: string;
}
