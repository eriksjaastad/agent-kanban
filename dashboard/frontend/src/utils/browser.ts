// Browser actions behind a seam, so tests replace them rather than relying on
// the DOM implementation letting window.location be spied on.
export const browser = {
  reload: () => window.location.reload(),
};
