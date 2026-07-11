import { create } from 'zustand';
import type { GroupOption } from '../api/types';

interface WorkshopState {
  groups: GroupOption[];
  selectedGroupId: string;
  selectGroup: (groupId: string) => void;
}

export const useWorkshopStore = create<WorkshopState>((set) => ({
  groups: [],
  selectedGroupId: '',
  selectGroup: (groupId) => set({ selectedGroupId: groupId }),
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
