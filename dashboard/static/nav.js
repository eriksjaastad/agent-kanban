// Dropdown behaviour for the Jinja nav (document pages such as /graph and /memory).
// Mirrors the React nav in dashboard/frontend/src/components/Navigation.tsx.
(function () {
  'use strict';

  function getCaret(group) {
    return group.querySelector('.nav-caret');
  }

  function openGroup(group) {
    document.querySelectorAll('.nav-group').forEach(function (other) {
      if (other !== group && other.classList.contains('nav-group--open')) {
        other.classList.remove('nav-group--open');
        var otherCaret = getCaret(other);
        if (otherCaret) {
          otherCaret.setAttribute('aria-expanded', 'false');
        }
      }
    });
    group.classList.add('nav-group--open');
    var caret = getCaret(group);
    if (caret) {
      caret.setAttribute('aria-expanded', 'true');
    }
  }

  function closeGroup(group) {
    group.classList.remove('nav-group--open');
    var caret = getCaret(group);
    if (caret) {
      caret.setAttribute('aria-expanded', 'false');
    }
  }

  function closeAllGroups() {
    document.querySelectorAll('.nav-group--open').forEach(closeGroup);
  }

  function init() {
    var groups = document.querySelectorAll('.nav-group');

    groups.forEach(function (group) {
      var caret = getCaret(group);
      // When a focusin opens a group, the caret click that follows on the same
      // user action (mouse mousedown-focus, keyboard Enter after Tab) must not
      // immediately toggle it closed again.
      var openedByFocus = false;
      // Escape closes the menu and moves focus back to the caret; that focus
      // must not be treated as "focus inside the group -> open it".
      var suppressFocusOpen = false;

      if (caret) {
        caret.addEventListener('click', function () {
          var focusOpenedIt = openedByFocus;
          openedByFocus = false;
          if (focusOpenedIt && group.classList.contains('nav-group--open')) {
            return; // The focusin already opened this group; this click confirms it.
          }
          var wasOpen = group.classList.contains('nav-group--open');
          closeAllGroups();
          if (!wasOpen) {
            openGroup(group);
          }
        });
      }

      group.addEventListener('pointerenter', function (event) {
        if (event.pointerType !== 'mouse') {
          return;
        }
        openedByFocus = false;
        openGroup(group);
      });

      group.addEventListener('pointerleave', function (event) {
        if (event.pointerType !== 'mouse') {
          return;
        }
        closeGroup(group);
      });

      group.addEventListener('focusin', function () {
        if (suppressFocusOpen) {
          suppressFocusOpen = false;
          return;
        }
        if (!group.classList.contains('nav-group--open')) {
          openedByFocus = true;
        }
        openGroup(group);
      });

      group.addEventListener('focusout', function (event) {
        if (event.relatedTarget && group.contains(event.relatedTarget)) {
          return; // Focus is moving inside the same group.
        }
        closeGroup(group);
      });

      group.addEventListener('keydown', function (event) {
        if (event.key === 'Escape') {
          event.stopPropagation();
          closeGroup(group);
          if (caret && document.activeElement !== caret) {
            suppressFocusOpen = true;
            caret.focus();
          }
        }
      });
    });

    document.addEventListener('pointerdown', function (event) {
      groups.forEach(function (group) {
        if (group.classList.contains('nav-group--open') && !group.contains(event.target)) {
          closeGroup(group);
        }
      });
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
