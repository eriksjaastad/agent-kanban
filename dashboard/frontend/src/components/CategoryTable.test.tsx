import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CategoryTable } from './CategoryTable';

describe('CategoryTable', () => {
  it('renders category breakdown table with counts and percentages', () => {
    const categories = [
      { category: 'Frontend/React', count: 10 },
      { category: 'Backend', count: 5 },
      { category: 'Full Stack', count: 3 },
    ];

    render(<CategoryTable categories={categories} />);

    expect(screen.getByText('Open Jobs by Category')).toBeInTheDocument();
    expect(screen.getByText('Current open listings grouped by role type (18 total)')).toBeInTheDocument();

    expect(screen.getByText('Frontend/React')).toBeInTheDocument();
    expect(screen.getByText('Backend')).toBeInTheDocument();
    expect(screen.getByText('Full Stack')).toBeInTheDocument();

    expect(screen.getByText('10')).toBeInTheDocument();
    expect(screen.getByText('5')).toBeInTheDocument();
    expect(screen.getByText('3')).toBeInTheDocument();

    expect(screen.getByText('55.6%')).toBeInTheDocument();
    expect(screen.getByText('27.8%')).toBeInTheDocument();
    expect(screen.getByText('16.7%')).toBeInTheDocument();
  });

  it('shows empty state when no categories exist', () => {
    const categories: Array<{ category: string; count: number }> = [];

    render(<CategoryTable categories={categories} />);

    expect(screen.getByText('No open jobs to categorize')).toBeInTheDocument();
  });

  it('handles single category correctly', () => {
    const categories = [{ category: 'Frontend/React', count: 7 }];

    render(<CategoryTable categories={categories} />);

    expect(screen.getByText('Frontend/React')).toBeInTheDocument();
    expect(screen.getByText('7')).toBeInTheDocument();
    expect(screen.getByText('100.0%')).toBeInTheDocument();
    expect(screen.getByText(/7 total/)).toBeInTheDocument();
  });

  it('calculates percentages correctly', () => {
    const categories = [
      { category: 'Backend', count: 1 },
      { category: 'Full Stack', count: 2 },
    ];

    render(<CategoryTable categories={categories} />);

    expect(screen.getByText('33.3%')).toBeInTheDocument();
    expect(screen.getByText('66.7%')).toBeInTheDocument();
  });

  it('displays table headers', () => {
    const categories = [{ category: 'Other', count: 1 }];

    render(<CategoryTable categories={categories} />);

    expect(screen.getByText('Category')).toBeInTheDocument();
    expect(screen.getByText('Count')).toBeInTheDocument();
    expect(screen.getByText('Percentage')).toBeInTheDocument();
  });
});
