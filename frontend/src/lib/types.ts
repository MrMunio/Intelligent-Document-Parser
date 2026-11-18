export type Role = "super_admin" | "admin" | "document_parser" | "phi_redactor" | "data_extractor";
export type Service = "parse" | "phi" | "extract";

export interface User {
  id: string;
  email: string;
  full_name: string;
  roles: Role[];
  services: Service[];
  is_active: boolean;
  must_change_password: boolean;
  last_login_at?: string | null;
  created_at?: string | null;
}

export interface TokenResponse {
  access_token: string;
  expires_in: number;
  user: User;
}

export type JobStatus = "PENDING" | "PROCESSING" | "COMPLETED" | "FAILED";
export type JobType = "PHI" | "PARSE" | "OCR" | "EXTRACT";

export interface JobProgress {
  stage?: string;
  percent?: number;
  detail?: string;
}

export interface Job {
  job_id: string;
  job_type: JobType;
  status: JobStatus;
  file_name?: string | null;
  input_format?: string | null;
  file_size?: number | null;
  source?: string | null;
  options?: Record<string, unknown> | null;
  progress?: JobProgress | null;
  stats?: Record<string, any> | null;
  error?: string | null;
  created_at?: string | null;
  started_at?: string | null;
  completed_at?: string | null;
  duration_ms?: number | null;
  owner_email?: string | null;
  result_file_name?: string | null;
  result_media_type?: string | null;
  result_available: boolean;
  links: Record<string, string>;
  usage?: UsageReport | null;
  result_data?: any;
  result_markdown?: string | null;
}

export interface UsageRow {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cost_usd: number | null;
  priced?: boolean;
}

/** Language model calls, tokens and cost of a job (the same shape for every service). */
export interface UsageReport {
  total: UsageRow;
  by_model: Record<string, UsageRow>;
}

export interface JobList {
  items: Job[];
  total: number;
  page: number;
  page_size: number;
}

export interface EntityType {
  label: string;
  group: string;
  description: string;
}

export interface PhiCatalog {
  entity_types: EntityType[];
  redaction_styles: string[];
  ocr_modes: string[];
  max_custom_entities: number;
  max_upload_mb: number;
  accepted_files: string;
}

export interface CustomEntity {
  label: string;
  description: string;
}

export interface ApiKey {
  id: string;
  name: string;
  prefix: string;
  scopes: Service[];
  created_at?: string | null;
  expires_at?: string | null;
  last_used_at?: string | null;
  revoked_at?: string | null;
  owner_email?: string | null;
}

export const ROLE_LABELS: Record<Role, string> = {
  super_admin: "Super admin",
  admin: "Admin",
  document_parser: "Document parser",
  phi_redactor: "PHI redactor",
  data_extractor: "Data extractor",
};

export const ROLE_DESCRIPTIONS: Record<Role, string> = {
  super_admin: "Full access, including managing administrators",
  admin: "All services, plus user management and system status",
  document_parser: "Document parsing only",
  phi_redactor: "PHI redaction only",
  data_extractor: "Data extraction only",
};

export const SERVICE_LABELS: Record<Service, string> = { parse: "Document parsing", phi: "PHI redaction", extract: "Data extraction" };

export const JOB_TYPE_LABELS: Record<JobType, string> = {
  PHI: "PHI redaction",
  PARSE: "Document parsing",
  OCR: "Text extraction",
  EXTRACT: "Data extraction",
};

// ---- Data extraction ---------------------------------------------------------

export type Approach = "agentic" | "batch";

export interface ExtractModel {
  provider: string;
  provider_name: string;
  id: string;
  key: string;
  display_name: string;
  context_window: number;
  max_output_tokens: number;
  tier: string;
  status: string;
  reasoning: boolean;
  pricing: { input?: number; output?: number; cached_input?: number };
  default: boolean;
  recommended_for_agentic: boolean;
}

export interface ExtractOptions {
  approaches: { value: Approach; description: string }[];
  default_model: string;
  models: ExtractModel[];
  context_threshold: number;
  context_cap_tokens: number | null;
  limits: { min_fields: number; max_fields: number; max_depth: number; max_upload_mb: number };
  supported_types: string[];
  accepted_files: string;
  example_spec: Record<string, unknown>;
}

export interface SpecIssue {
  path: string;
  message: string;
}

export interface SpecReport {
  valid: boolean;
  errors: SpecIssue[];
  warnings: string[];
  field_count: number;
  fields: { path: string; type: string; description: string; required?: boolean; enum?: unknown[]; items?: string; format?: string }[];
}

export interface ExtractField {
  path: string;
  type: string;
  status: "found" | "not_found" | "pending";
  value: unknown;
  pages: number[];
  confidence?: string | null;
  note?: string;
  session?: number | null;
}
