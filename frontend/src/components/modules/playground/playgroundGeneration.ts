import type { PlaygroundGenerationResponse } from '@/lib/api';
import type { PlaygroundGeneration, PlaygroundMode } from './usePlaygroundStore';

/** Convert the API contract into the client-side generation shape shared by
 * the composer and the dedicated creation-history page. */
export function toPlaygroundGeneration(
  response: PlaygroundGenerationResponse,
): PlaygroundGeneration {
  return {
    id: response.id,
    mode: response.mode as PlaygroundMode,
    model_id: response.model_id,
    actual_model_name: response.actual_model_name,
    actual_model_id: response.actual_model_id,
    prompt: response.prompt,
    negative_prompt: response.negative_prompt,
    input_media: response.input_media,
    parameters: response.parameters,
    batch_size: response.batch_size,
    outputs: response.outputs.map((output) => ({
      id: output.id,
      media_reference: output.media_reference,
      media_id: output.media_id,
      media_url: output.media_url,
      media_type: output.media_type as 'image' | 'video',
      thumbnail_path: output.thumbnail_path,
      saved_to_library: output.saved_to_library,
    })),
    status: response.status as PlaygroundGeneration['status'],
    raw_status: response.raw_status,
    status_zh: response.status_zh,
    cancellation_requested: response.cancellation_requested,
    support_review: response.support_review,
    support_review_reason: response.support_review_reason,
    error: response.error,
    provider_name: response.provider_name,
    provider_task_id: response.provider_task_id,
    provider_request_id: response.provider_request_id,
    created_at: response.created_at,
    quoted_microtickets: response.quoted_microtickets,
    quoted_tickets: response.quoted_tickets,
    tokens_per_ticket: response.tokens_per_ticket,
  };
}
