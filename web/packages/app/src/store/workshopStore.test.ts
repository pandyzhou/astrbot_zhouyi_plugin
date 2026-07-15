import assert from 'node:assert/strict';
import test from 'node:test';
import { useWorkshopStore } from './workshopStore';

test('趋势筛选按群组保留，页面切换后不会重置', () => {
  useWorkshopStore.setState({
    groups: [],
    selectedGroupId: '10001',
    trendFiltersByGroup: {},
  });

  const kaFilters = {
    serverId: 'ka',
    hours: 72,
    hoursInput: '72',
    settingsReady: true,
  };
  useWorkshopStore.getState().setTrendFilters('10001', kaFilters);
  useWorkshopStore.getState().selectGroup('10002');
  useWorkshopStore.getState().selectGroup('10001');

  assert.deepEqual(useWorkshopStore.getState().trendFiltersByGroup['10001'], kaFilters);
});
