import { create } from 'zustand';
import type { GroupOption } from '../api/types';

export interface TrendFiltersState {
  serverId: string;
  hours: number;
  hoursInput: string;
  settingsReady: boolean;
}

interface WorkshopState {
  groups: GroupOption[];
  selectedGroupId: string;
  trendFiltersByGroup: Record<string, TrendFiltersState>;
  selectGroup: (groupId: string) => void;
  setTrendFilters: (groupId: string, filters: TrendFiltersState) => void;
}

export const useWorkshopStore = create<WorkshopState>((set) => ({
  groups: [],
  selectedGroupId: '',
  trendFiltersByGroup: {},
  selectGroup: (groupId) => set({ selectedGroupId: groupId }),
  setTrendFilters: (groupId, filters) => set((state) => ({
    trendFiltersByGroup: { ...state.trendFiltersByGroup, [groupId]: filters },
  })),
}));

export function initializeGroups(groups: GroupOption[], defaultGroupId: string | null) {
  useWorkshopStore.setState((state) => {
    const selectedStillExists = groups.some((group) => group.group_id === state.selectedGroupId);
    return {
      groups,
      selectedGroupId: selectedStillExists
        ? state.selectedGroupId
        : defaultGroupId ?? groups[0]?.group_id ?? '',
    };
  });
}
