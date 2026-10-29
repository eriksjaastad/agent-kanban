import { useEffect, useRef, useState } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { fetchNavigation } from '../api';
import { useRequest } from '../hooks/useRequest';
import type { NavigationItem, NavigationResponse } from '../types';
import './Navigation.css';

declare global {
  interface Window {
    __PT_NAVIGATION__?: NavigationResponse;
  }
}

function getInitialNavigation(): NavigationResponse {
  return window.__PT_NAVIGATION__ ?? { title: 'Project Tracker', items: [] };
}

function longestMatchLength(item: NavigationItem, pathname: string) {
  return item.match_prefixes.reduce(
    (best, prefix) =>
      pathname === prefix || pathname.startsWith(`${prefix}/`) ? Math.max(best, prefix.length) : best,
    -1,
  );
}

function isNavigationItemActive(item: NavigationItem, pathname: string) {
  return longestMatchLength(item, pathname) >= 0;
}

// Within a group the most specific match wins, as in the server's
// build_navigation: /jobs/submitted highlights Submissions, not Listings too.
function activeChildIds(children: NavigationItem[], pathname: string) {
  const lengths = children.map(child => longestMatchLength(child, pathname));
  const best = Math.max(...lengths);
  return new Set(best < 0 ? [] : children.filter((_, index) => lengths[index] === best).map(child => child.id));
}

export function Navigation() {
  const location = useLocation();
  const [initialNavigation] = useState<NavigationResponse>(() => getInitialNavigation());
  const { data: fetchedNavigation, error: navigationError } = useRequest(fetchNavigation, []);
  const navigation = fetchedNavigation ?? initialNavigation;
  // The open group is recorded together with the pathname it was opened on, so
  // a location change (SPA navigation, back/forward) closes any open menu
  // without a setState-in-effect. The menu is only visible when the recorded
  // pathname still matches the current one.
  const [openState, setOpenState] = useState<{ groupId: string | null; pathname: string }>({
    groupId: null,
    pathname: location.pathname,
  });
  const openGroupId =
    openState.pathname === location.pathname ? openState.groupId : null;
  const caretRefs = useRef<Record<string, HTMLButtonElement | null>>({});
  const navRef = useRef<HTMLElement | null>(null);
  // When a focus event opens a group, the caret click that follows on the same
  // user action (mouse mousedown-focus, keyboard Enter after Tab) must not
  // immediately toggle it closed again.
  const openedByFocusRef = useRef(false);
  // Escape closes the menu and moves focus back to the caret; that focus must
  // not be treated as "focus inside the group → open it" or the menu reopens.
  const suppressFocusOpenRef = useRef(false);

  useEffect(() => {
    if (navigationError) console.error('Failed to load navigation metadata:', navigationError);
  }, [navigationError]);

  useEffect(() => {
    if (openGroupId === null) {
      return undefined;
    }

    function handlePointerDown(event: PointerEvent) {
      const openGroup = navRef.current?.querySelector('.nav-group--open');
      if (!openGroup) {
        return;
      }
      const target = event.target;
      if (target instanceof Node && !openGroup.contains(target)) {
        openedByFocusRef.current = false;
        setOpenState((current) => ({ groupId: null, pathname: current.pathname }));
      }
    }

    document.addEventListener('pointerdown', handlePointerDown);
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown);
    };
  }, [openGroupId]);

  function openGroupFromFocus(id: string) {
    if (suppressFocusOpenRef.current) {
      suppressFocusOpenRef.current = false;
      return;
    }
    if (openGroupId !== id) {
      openedByFocusRef.current = true;
      setOpenState({ groupId: id, pathname: location.pathname });
    }
  }

  function toggleGroup(id: string) {
    const focusOpenedIt = openedByFocusRef.current;
    openedByFocusRef.current = false;
    if (focusOpenedIt && openGroupId === id) {
      return; // The focus event already opened this group; this click confirms it.
    }
    setOpenState({
      groupId: openGroupId === id ? null : id,
      pathname: location.pathname,
    });
  }

  function closeGroup() {
    openedByFocusRef.current = false;
    setOpenState({ groupId: null, pathname: location.pathname });
  }

  function handleGroupBlur(event: React.FocusEvent<HTMLDivElement>) {
    const nextTarget = event.relatedTarget as Node | null;
    if (nextTarget && event.currentTarget.contains(nextTarget)) {
      return; // Focus is moving inside the same group.
    }
    closeGroup();
  }

  function handleGroupKeyDown(event: React.KeyboardEvent<HTMLDivElement>, id: string) {
    if (event.key === 'Escape') {
      event.stopPropagation();
      closeGroup();
      suppressFocusOpenRef.current = true;
      caretRefs.current[id]?.focus();
    }
  }

  return (
    <nav className="navigation" ref={navRef}>
      <div className="navigation-container">
        <div className="navigation-brand">
          <Link to="/dashboard" className="navigation-title" onClick={closeGroup}>
            {navigation.title}
          </Link>
        </div>
        <div className="navigation-links">
          {navigation.items.map((item) => {
            if (item.children && item.children.length > 0) {
              const activeIds = activeChildIds(item.children, location.pathname);
              const groupActive = activeIds.size > 0;
              const open = openGroupId === item.id;
              const firstChild = item.children[0];
              const linkClassName = `nav-link ${groupActive ? 'active' : ''}`;
              const groupClassName = `nav-group ${open ? 'nav-group--open' : ''}`;

              return (
                <div
                  key={item.id}
                  className={groupClassName}
                  onPointerEnter={(event) => {
                    if (event.pointerType !== 'mouse') {
                      return;
                    }
                    openedByFocusRef.current = false;
                    setOpenState({ groupId: item.id, pathname: location.pathname });
                  }}
                  onPointerLeave={(event) => {
                    if (event.pointerType !== 'mouse') {
                      return;
                    }
                    if (open) {
                      closeGroup();
                    }
                  }}
                  onFocus={() => openGroupFromFocus(item.id)}
                  onBlur={handleGroupBlur}
                  onKeyDown={(event) => handleGroupKeyDown(event, item.id)}
                >
                  {firstChild.navigation_type === 'spa' ? (
                    <Link to={item.href} className={linkClassName} onClick={closeGroup}>
                      {item.label}
                    </Link>
                  ) : (
                    <a href={item.href} className={linkClassName} onClick={closeGroup}>
                      {item.label}
                    </a>
                  )}
                  <button
                    type="button"
                    className="nav-caret"
                    aria-haspopup="true"
                    aria-expanded={open}
                    aria-label={`${item.label} menu`}
                    onClick={() => toggleGroup(item.id)}
                    ref={(el) => {
                      caretRefs.current[item.id] = el;
                    }}
                  >
                    ▾
                  </button>
                  <div className="nav-menu" role="menu">
                    {item.children.map(child => {
                      const childActive = activeIds.has(child.id);
                      const childClassName = `nav-menu-link ${childActive ? 'active' : ''}`;

                      if (child.navigation_type === 'spa') {
                        return (
                          <Link key={child.id} to={child.href} role="menuitem" className={childClassName} onClick={closeGroup}>
                            {child.label}
                          </Link>
                        );
                      }

                      return (
                        <a key={child.id} href={child.href} role="menuitem" className={childClassName} onClick={closeGroup}>
                          {child.label}
                        </a>
                      );
                    })}
                  </div>
                </div>
              );
            }

            const className = `nav-link ${
              isNavigationItemActive(item, location.pathname) ? 'active' : ''
            }`;

            if (item.navigation_type === 'spa') {
              return (
                <Link key={item.id} to={item.href} className={className} onClick={closeGroup}>
                  {item.label}
                </Link>
              );
            }

            return (
              <a key={item.id} href={item.href} className={className} onClick={closeGroup}>
                {item.label}
              </a>
            );
          })}
        </div>
      </div>
    </nav>
  );
}
