import type { ReactNode } from 'react';

type ContentWidth = 'default' | 'narrow' | 'full';

interface PageShellProps {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
  children: ReactNode;
  contentWidth?: ContentWidth;
  headerWidth?: Exclude<ContentWidth, 'full'>;
  mainClassName?: string;
}

function joinClassNames(...classNames: Array<string | false | null | undefined>) {
  return classNames.filter(Boolean).join(' ');
}

export function PageShell({
  title,
  subtitle,
  actions,
  children,
  contentWidth = 'default',
  headerWidth = 'default',
  mainClassName,
}: PageShellProps) {
  return (
    <>
      <header className="page-header">
        <div
          className={joinClassNames(
            'header-content',
            headerWidth === 'narrow' && 'header-content--narrow'
          )}
        >
          <div className="page-title-group">
            <h1>{title}</h1>
            {subtitle ? <p className="page-subtitle">{subtitle}</p> : null}
          </div>
          {actions ? <div className="header-actions">{actions}</div> : null}
        </div>
      </header>

      <main
        className={joinClassNames(
          'page-main',
          contentWidth === 'narrow' && 'page-main--narrow',
          contentWidth === 'full' && 'page-main--full',
          mainClassName
        )}
      >
        {children}
      </main>
    </>
  );
}