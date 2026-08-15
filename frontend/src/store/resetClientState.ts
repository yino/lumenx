import { usePlaygroundStore } from "@/components/modules/playground/usePlaygroundStore";
import { useProjectStore } from "@/store/projectStore";

export function resetClientStateForScope(): void {
  useProjectStore.getState().resetForScope();
  usePlaygroundStore.getState().resetForScope();
}
