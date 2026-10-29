import { Component, type ReactNode } from 'react';
import { useLocation } from 'react-router-dom';

import { browser } from '../utils/browser';

interface Props {
  children: ReactNode;
}

interface BoundaryProps extends Props {
  pathname: string;
}

interface State {
  failed: boolean;
  pathname: string;
}

/**
 * Catches a page that fails to load or render, so the rest of the app stays.
 *
 * The chart pages load as separate chunks, and a rebuild replaces those
 * chunks. A tab opened before a redeploy (or offline) can't fetch them; without
 * this the whole app, navigation included, would unmount to a blank page.
 */
class RouteErrorBoundary extends Component<BoundaryProps, State> {
  state: State = { failed: false, pathname: this.props.pathname };

  static getDerivedStateFromError(): Partial<State> {
    return { failed: true };
  }

  // Moving to another page clears a failure. The boundary is not keyed by
  // path: that would remount the page on /kanban -> /kanban/:project and lose
  // its state, which the router itself keeps.
  static getDerivedStateFromProps(props: BoundaryProps, state: State): Partial<State> | null {
    return props.pathname === state.pathname ? null : { failed: false, pathname: props.pathname };
  }

  componentDidCatch(error: unknown) {
    console.error('Page failed to load:', error);
  }

  render() {
    if (!this.state.failed) {
      return this.props.children;
    }
    return (
      <div className="route-error" role="alert">
        <p>This page couldn't load. If the dashboard was updated since this tab was opened, reloading fixes it.</p>
        <button type="button" onClick={() => browser.reload()}>
          Reload
        </button>
      </div>
    );
  }
}

/** The pages' boundary; a failure clears when the path changes. */
export function PageBoundary({ children }: Props) {
  const { pathname } = useLocation();
  return <RouteErrorBoundary pathname={pathname}>{children}</RouteErrorBoundary>;
}
