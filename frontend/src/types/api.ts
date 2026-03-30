// LibraryAI API types.
// These types are based on the documented frontend contract, with a few
// compatibility fields so the frontend can tolerate minor response-shape drift
// from the live backend without scattering that logic through page components.

// ---- Auth -------------------------------------------------------

export interface LoginRequest {
  username: string;
  password: string;
}

export interface LoginResponse {
  access_token: string;
  token_type: "bearer";
}

export interface JWTPayload {
  sub: string;
  role: "user" | "admin";
  exp: number;
}

export interface SignupRequest {
  username: string;
  password: string;
}

export interface SignupResponse {
  user_id: string;
  username: string;
  role: "user";
  is_active: boolean;
  created_at: string;
}

export interface AuthSession {
  token: string;
  username: string;
  userId: string;
  role: "user" | "admin";
  exp: number;
}

// ---- Upload -------------------------------------------------------

export interface PresignUploadResponse {
  upload_url: string;
  object_uri: string;
  expires_in: number;
}

// ---- Jobs -------------------------------------------------------

export type MaterialType =
  | "book"
  | "newspaper"
  | "archival_document"
  | "document";

export type PipelineMode = "preprocess" | "layout";
export type PtiffQaMode = "manual" | "auto_continue";
export type JobStatus = "queued" | "running" | "done" | "failed";

export type PageState =
  | "queued"
  | "preprocessing"
  | "rectification"
  | "ptiff_qa_pending"
  | "layout_detection"
  | "pending_human_correction"
  | "accepted"
  | "review"
  | "failed"
  | "split";

export interface PageInput {
  page_number: number;
  input_uri: string;
}

export interface CreateJobRequest {
  collection_id: string;
  material_type: MaterialType;
  pages: PageInput[];
  pipeline_mode: PipelineMode;
  ptiff_qa_mode: PtiffQaMode;
  policy_version: string;
  shadow_mode?: boolean;
}

export interface CreateJobResponse {
  job_id: string;
  status: JobStatus;
  page_count: number;
  created_at: string;
}

export interface JobSummary {
  job_id: string;
  collection_id: string;
  material_type: MaterialType;
  pipeline_mode: PipelineMode;
  ptiff_qa_mode: PtiffQaMode;
  policy_version: string;
  shadow_mode: boolean;
  created_by: string | null;
  created_by_username: string | null;
  status: JobStatus;
  page_count: number;
  accepted_count: number;
  review_count: number;
  failed_count: number;
  pending_human_correction_count: number;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
}

export interface QualitySummary {
  blur_score: number | null;
  border_score: number | null;
  skew_residual: number | null;
  foreground_coverage: number | null;
}

export interface JobPage {
  page_number: number;
  sub_page_index: number | null;
  status: PageState;
  routing_path: string | null;
  output_image_uri: string | null;
  output_layout_uri: string | null;
  quality_summary: QualitySummary | null;
  review_reasons: string[] | null;
  acceptance_decision: string | null;
  processing_time_ms: number | null;
}

export interface JobDetailResponse {
  summary: JobSummary;
  pages: JobPage[];
}

export interface JobsListResponse {
  total: number;
  page: number;
  page_size: number;
  items: JobSummary[];
}

export interface JobsListParams {
  search?: string;
  status?: JobStatus;
  pipeline_mode?: PipelineMode;
  created_by?: string;
  from_date?: string;
  to_date?: string;
  page?: number;
  page_size?: number;
}

// ---- PTIFF QA -------------------------------------------------------

export type PtiffApprovalStatus = "pending" | "approved" | "in_correction";

export interface PtiffQaPage {
  page_number: number;
  sub_page_index: number | null;
  current_state: PageState;
  approval_status: PtiffApprovalStatus;
  needs_correction: boolean;
}

export interface PtiffQaResponse {
  job_id: string;
  ptiff_qa_mode: PtiffQaMode;
  total_pages: number;
  pages_pending: number;
  pages_approved: number;
  pages_in_correction: number;
  is_gate_ready: boolean;
  pages: PtiffQaPage[];
}

export interface PtiffApproveAllResponse {
  approved_count: number;
  gate_released: boolean;
}

export interface PtiffApprovePageResponse {
  page_number: number;
  approved: boolean;
  gate_released: boolean;
}

export interface PtiffEditPageResponse {
  page_number: number;
  new_state: PageState;
}

// ---- Correction Queue -------------------------------------------------------

export interface CorrectionQueueItem {
  job_id: string;
  page_number: number;
  sub_page_index: number | null;
  material_type: MaterialType;
  pipeline_mode: PipelineMode;
  review_reasons: string[];
  waiting_since: string | null;
  output_image_uri: string | null;
}

export interface CorrectionQueueResponse {
  total: number;
  offset: number;
  limit: number;
  page: number;
  page_size: number;
  items: CorrectionQueueItem[];
}

export interface CorrectionQueueParams {
  job_id?: string;
  material_type?: MaterialType;
  review_reason?: string;
  page?: number;
  page_size?: number;
}

export interface BranchGeometryOutput {
  page_count: number;
  split_required: boolean;
  geometry_confidence: number | null;
}

export interface BranchOutputs {
  iep1a_geometry: BranchGeometryOutput | null;
  iep1b_geometry: BranchGeometryOutput | null;
  iep1c_normalized: string | null;
  iep1d_rectified: string | null;
}

export interface CorrectionWorkspaceDetail {
  job_id: string;
  page_number: number;
  sub_page_index: number | null;
  material_type: MaterialType;
  pipeline_mode: PipelineMode;
  review_reasons: string[];
  original_otiff_uri: string | null;
  best_output_uri: string | null;
  branch_outputs: BranchOutputs;
  current_crop_box: [number, number, number, number] | null;
  current_deskew_angle: number | null;
  current_split_x: number | null;
}

export interface SubmitCorrectionRequest {
  crop_box: [number, number, number, number] | null;
  deskew_angle: number | null;
  split_x: number | null;
  notes?: string | null;
}

export interface SubmitCorrectionResponse {
  status?: string;
  page_id?: string;
  new_state?: PageState;
}

export interface RejectPageResponse {
  page_number?: number;
  page_id?: string;
  new_state: PageState | string;
}

// ---- Lineage -------------------------------------------------------

export interface LineageServiceInvocation {
  id: number;
  lineage_id: string;
  service_name: string;
  service_version: string | null;
  model_version: string | null;
  model_source: string | null;
  invoked_at: string;
  completed_at: string | null;
  processing_time_ms: number | null;
  status: string;
  error_message: string | null;
  metrics: unknown;
  config_snapshot: unknown;
}

export interface LineageQualityGate {
  gate_id: string;
  job_id: string;
  page_number: number;
  gate_type: string;
  iep1a_geometry: unknown;
  iep1b_geometry: unknown;
  structural_agreement: boolean | null;
  selected_model: string | null;
  selection_reason: string | null;
  sanity_check_results: unknown;
  split_confidence: unknown;
  tta_variance: unknown;
  artifact_validation_score: number | null;
  route_decision: string;
  review_reason: string | null;
  processing_time_ms: number | null;
  created_at: string;
}

export interface LineageRecord {
  lineage_id: string;
  job_id: string;
  page_number: number;
  sub_page_index: number | null;
  correlation_id: string;
  input_image_uri: string;
  input_image_hash: string | null;
  otiff_uri: string;
  reference_ptiff_uri: string | null;
  ptiff_ssim: number | null;
  iep1a_used: boolean;
  iep1b_used: boolean;
  selected_geometry_model: string | null;
  structural_agreement: boolean | null;
  iep1d_used: boolean;
  material_type: string;
  routing_path: string | null;
  policy_version: string;
  acceptance_decision: string | null;
  acceptance_reason: string | null;
  gate_results: unknown;
  total_processing_ms: number | null;
  shadow_eval_id: string | null;
  cleanup_retry_count: number;
  preprocessed_artifact_state: string;
  layout_artifact_state: string;
  output_image_uri: string | null;
  parent_page_id: string | null;
  split_source: boolean;
  human_corrected: boolean;
  human_correction_timestamp: string | null;
  human_correction_fields: unknown;
  reviewed_by: string | null;
  reviewed_at: string | null;
  reviewer_notes: string | null;
  created_at: string;
  completed_at: string | null;
  service_invocations: LineageServiceInvocation[];
}

export interface LineageResponse {
  job_id: string;
  page_number: number;
  lineage: LineageRecord[];
  quality_gates: LineageQualityGate[];
}

// ---- Admin -------------------------------------------------------

export interface DashboardSummary {
  throughput_pages_per_hour: number | null;
  auto_accept_rate: number | null;
  structural_agreement_rate: number | null;
  pending_corrections_count: number;
  active_jobs_count: number;
  active_workers_count: number;
  shadow_evaluations_count: number;
  total_jobs?: number | null;
  active_jobs?: number | null;
  total_pages?: number | null;
  pending_human_correction_total?: number | null;
  shadow_jobs?: number | null;
  pages_by_state?: Record<string, number> | null;
  as_of?: string | null;
}

export interface ServiceHealthRate {
  key:
    | "preprocessing_success_rate"
    | "rectification_success_rate"
    | "layout_success_rate"
    | "human_review_throughput_rate"
    | "structural_agreement_rate";
  label: string;
  value: number | null;
}

export interface ServiceHealthResponse {
  preprocessing_success_rate?: number | null;
  rectification_success_rate?: number | null;
  layout_success_rate?: number | null;
  human_review_throughput_rate?: number | null;
  structural_agreement_rate?: number | null;
  window_hours?: number | null;
  services?: Array<{
    service_name: string;
    status: "healthy" | "degraded" | "down" | "unknown";
    last_check: string | null;
    latency_ms: number | null;
    error_message: string | null;
  }>;
  as_of?: string | null;
}

// ---- Policy -------------------------------------------------------

export interface PolicyRecord {
  version: string;
  config_yaml: string;
  applied_at: string;
  applied_by: string | null;
  justification: string | null;
}

export interface UpdatePolicyRequest {
  config_yaml: string;
  justification: string;
  version: string;
}

// ---- Model Management -------------------------------------------------------

export type ModelStage =
  | "experimental"
  | "staging"
  | "shadow"
  | "production"
  | "archived";

export interface GateResult {
  pass: boolean;
  value: number | null;
}

export interface GateSummary {
  total_gates: number;
  passed_gates: number;
  failed_gates: number;
  all_pass: boolean;
  failed_names: string[];
}

export interface ModelVersionRecord {
  model_id: string;
  service_name: string;
  version_tag: string;
  stage: ModelStage;
  dataset_version: string | null;
  mlflow_run_id: string | null;
  gate_results: Record<string, GateResult> | null;
  gate_summary: GateSummary | null;
  promoted_at: string | null;
  notes: string | null;
  created_at: string;
}

export interface ModelEvaluationResponse {
  total: number;
  records: ModelVersionRecord[];
}

export interface ModelEvaluationParams {
  candidate_tag?: string;
  service?: string;
  stage?: ModelStage;
  limit?: number;
}

export interface TriggerEvaluationRequest {
  candidate_tag: string;
  service: string;
}

export interface TriggerEvaluationResponse {
  evaluation_job_id: string;
  model_id: string;
  service_name: string;
  version_tag: string;
  status: "pending";
  message: string;
}

export interface PromoteModelRequest {
  service: "iep1a" | "iep1b";
  force: boolean;
}

export interface RollbackModelRequest {
  service: "iep1a" | "iep1b";
  reason: string;
}

// ---- Retraining -------------------------------------------------------

export type RetrainingJobStatus = "pending" | "running" | "completed" | "failed";

export interface RetrainingJobSummary {
  job_id: string;
  pipeline_type: string;
  status: RetrainingJobStatus;
  trigger_id: string | null;
  dataset_version: string | null;
  mlflow_run_id: string | null;
  result_mAP: number | null;
  promotion_decision: string | null;
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  created_at: string;
}

export interface TriggerCooldown {
  trigger_type: string;
  in_cooldown: boolean;
  cooldown_until: string | null;
  last_fired_at: string | null;
  last_status: string | null;
}

export interface RetrainingStatusSummary {
  active_count: number;
  queued_count: number;
  completed_count: number;
  failed_count: number;
  total_triggers: number;
  pending_triggers: number;
}

export interface RetrainingStatusResponse {
  summary: RetrainingStatusSummary;
  active_jobs: RetrainingJobSummary[];
  queued_jobs: RetrainingJobSummary[];
  recently_completed: RetrainingJobSummary[];
  trigger_cooldowns: TriggerCooldown[];
  as_of: string;
}

// ---- Users -------------------------------------------------------

export interface UserRecord {
  user_id: string;
  username: string;
  role: "user" | "admin";
  is_active: boolean;
  created_at: string;
}

export interface CreateUserRequest {
  username: string;
  password: string;
  role: "user" | "admin";
}

export interface UsersListResponse {
  total: number;
  items: UserRecord[];
}

// ---- Artifacts -------------------------------------------------------

export interface PresignReadRequest {
  uri: string;
  expires_in?: number;
}

export interface PresignReadResponse {
  uri: string;
  read_url: string;
  expires_in: number;
  content_type_hint: string;
}
