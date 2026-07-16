import { ProjectConversationState } from "../../api/client";
import { ChatMessage } from "../../lib/chatStore";
import {
  buildApprovalMessage,
  generationResultMessage,
  runtimeApprovalMessage,
} from "./ChatWorkspace";

export function mergeProjectConversationState(
  messages: ChatMessage[],
  state: ProjectConversationState,
): ChatMessage[] {
  let merged = [...messages];
  const generation = state.generation_request;
  if (state.permission_request) {
    const status = state.permission_request.status;
    merged = upsert(
      merged,
      (message) => message.kind === "build_approval" && message.generationRequest?.id === generation.id,
      {
        id: recoveredMessageId(generation.id, 1),
        role: "assistant",
        kind: "build_approval",
        generationRequest: generation,
        permissionRequest: state.permission_request,
        actionStatus: status,
        content: `${buildApprovalMessage(generation.proposed_display_name)}${statusText(status)}`,
      },
    );
  }
  if (state.proposed_skill) {
    merged = upsert(
      merged,
      (message) => message.kind === "generation_result" && message.skill?.id === state.proposed_skill?.id,
      {
        id: recoveredMessageId(generation.id, 2),
        role: "assistant",
        kind: "generation_result",
        skill: state.proposed_skill,
        agentRun: state.agent_run,
        content: generationResultMessage(state.proposed_skill.name, state.agent_run, null),
      },
    );
  }
  if (state.proposed_skill && state.runtime_permission_request) {
    const status = state.runtime_permission_request.status;
    merged = upsert(
      merged,
      (message) =>
        message.kind === "runtime_approval" &&
        message.permissionRequest?.id === state.runtime_permission_request?.id,
      {
        id: recoveredMessageId(state.runtime_permission_request.id, 3),
        role: "assistant",
        kind: "runtime_approval",
        skill: state.proposed_skill,
        permissionRequest: state.runtime_permission_request,
        actionStatus: status,
        content: `${runtimeApprovalMessage(state.proposed_skill.name, state.runtime_permission_request)}${statusText(status)}`,
      },
    );
  }
  return merged;
}

function upsert(
  messages: ChatMessage[],
  predicate: (message: ChatMessage) => boolean,
  recovered: ChatMessage,
): ChatMessage[] {
  const index = messages.findIndex(predicate);
  if (index < 0) return [...messages, recovered];
  const current = messages[index];
  const next = {
    ...current,
    generationRequest: recovered.generationRequest ?? current.generationRequest,
    permissionRequest: recovered.permissionRequest ?? current.permissionRequest,
    skill: recovered.skill ?? current.skill,
    agentRun: recovered.agentRun ?? current.agentRun,
    actionStatus: recovered.actionStatus,
  };
  const copy = [...messages];
  copy[index] = next;
  return copy;
}

function recoveredMessageId(recordId: number, suffix: number): number {
  return -(recordId * 10 + suffix);
}

function statusText(status: string): string {
  if (status === "pending") return "";
  return `\n\nStatus synchronized from the backend: ${status}.`;
}
