import { apiGet, apiPost } from "./client";
import type {
  ModelEvaluationParams,
  ModelEvaluationResponse,
  ModelVersionRecord,
  TriggerEvaluationRequest,
  TriggerEvaluationResponse,
  PromoteModelRequest,
  RollbackModelRequest,
} from "@/types/api";

export function getModelEvaluations(
  params?: ModelEvaluationParams
): Promise<ModelEvaluationResponse> {
  return apiGet<ModelEvaluationResponse>(
    "/v1/models/evaluation",
    params as Record<string, unknown>
  );
}

export function triggerEvaluation(
  data: TriggerEvaluationRequest
): Promise<TriggerEvaluationResponse> {
  return apiPost<TriggerEvaluationResponse>("/v1/models/evaluate", data);
}

export function promoteModel(data: PromoteModelRequest): Promise<ModelVersionRecord> {
  return apiPost<ModelVersionRecord>("/v1/models/promote", data);
}

export function rollbackModel(data: RollbackModelRequest): Promise<ModelVersionRecord> {
  return apiPost<ModelVersionRecord>("/v1/models/rollback", data);
}
