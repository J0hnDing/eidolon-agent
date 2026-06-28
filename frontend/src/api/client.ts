const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

export type MemoryCategory =
  | "interests"
  | "goals"
  | "preferences"
  | "routines"
  | "trusted_sources"
  | "blocked_sources"
  | "writing_style"
  | "risk_tolerance";

export type RiskLevel = "low" | "medium" | "high";
export type SkillStatus = "proposed" | "installed" | "disabled" | "failed" | "deleted";

export interface MemoryFact {
  id: number;
  key: string;
  value: string;
  category: MemoryCategory;
  source_message_id: number | null;
  sensitivity: string;
  expires_at: string | null;
  user_editable: boolean;
  created_at: string;
  updated_at: string;
}

export type MemoryFactInput = Omit<MemoryFact, "id" | "created_at" | "updated_at">;

export interface Skill {
  id: number;
  name: string;
  description: string;
  status: SkillStatus;
  risk_level: RiskLevel;
  manifest_path: string;
  installed_path: string | null;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export type SkillInput = Omit<Skill, "id" | "created_at" | "updated_at">;

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...options.headers,
    },
  });

  if (!response.ok) {
    const message = await response.text();
    throw new Error(message || `Request failed with status ${response.status}`);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}

export const api = {
  listMemoryFacts: () => request<MemoryFact[]>("/memory-facts"),
  createMemoryFact: (payload: MemoryFactInput) =>
    request<MemoryFact>("/memory-facts", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateMemoryFact: (id: number, payload: Partial<MemoryFactInput>) =>
    request<MemoryFact>(`/memory-facts/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteMemoryFact: (id: number) =>
    request<void>(`/memory-facts/${id}`, {
      method: "DELETE",
    }),
  listSkills: () => request<Skill[]>("/skills"),
  createSkill: (payload: SkillInput) =>
    request<Skill>("/skills", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  getSkill: (id: number) => request<Skill>(`/skills/${id}`),
  updateSkill: (id: number, payload: Partial<SkillInput>) =>
    request<Skill>(`/skills/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteSkill: (id: number) =>
    request<Skill>(`/skills/${id}`, {
      method: "DELETE",
    }),
};
