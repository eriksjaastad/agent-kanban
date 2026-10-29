import './CategoryTable.css';

interface CategoryData {
  category: string;
  count: number;
}

interface Props {
  categories: CategoryData[];
}

export function CategoryTable({ categories }: Props) {
  const totalJobs = categories.reduce((sum, cat) => sum + cat.count, 0);
  const hasData = categories.length > 0;

  return (
    <section className="category-table-section" aria-label="Jobs by category">
      <div className="category-table-heading">
        <h2>Open Jobs by Category</h2>
        <p className="category-table-subtitle">
          Current open listings grouped by role type ({totalJobs} total)
        </p>
      </div>
      {hasData ? (
        <div className="category-table-container">
          <table className="category-table">
            <thead>
              <tr>
                <th>Category</th>
                <th>Count</th>
                <th>Percentage</th>
              </tr>
            </thead>
            <tbody>
              {categories.map(cat => {
                const percentage = totalJobs > 0 ? ((cat.count / totalJobs) * 100).toFixed(1) : '0.0';
                return (
                  <tr key={cat.category}>
                    <td className="category-name">{cat.category}</td>
                    <td className="category-count">{cat.count}</td>
                    <td className="category-percentage">{percentage}%</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      ) : (
        <p className="category-table-empty">No open jobs to categorize</p>
      )}
    </section>
  );
}
