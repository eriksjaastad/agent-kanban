import { render, screen } from '@testing-library/react';
import { DndContext } from '@dnd-kit/core';
import { SortableContext } from '@dnd-kit/sortable';
import { describe, expect, it } from 'vitest';

import type { Task } from '../types';
import { TaskCard } from './TaskCard';

const task = { id: '1', display_id: 1, text: 'Wire the tab bar', project_id: 'holoapp', status: 'To Do' } as Task;

describe('TaskCard', () => {
  it("badges a card whose project is checked out (#8339)", () => {
    render(
      <DndContext>
        <SortableContext items={['1']}>
          <TaskCard
            task={task}
            checkout={{ machine: 'mini', machine_label: 'Mac Mini', holder: 'Qwen on Mini', note: null, checked_out_at: '2026-10-16' }}
          />
        </SortableContext>
      </DndContext>
    );
    expect(screen.getByText('Checked out · Mac Mini')).toBeInTheDocument();
  });
});
