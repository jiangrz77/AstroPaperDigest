/* MathJax configuration for digest titles, abstracts, and relevance reasons.
 * This file is deliberately standalone rather than embedded in a Python HTML
 * string, so TeX backslashes reach JavaScript unchanged. */
window.MathJax = {
  tex: {
    inlineMath: [['$', '$'], ['\\(', '\\)']],
    displayMath: [['$$', '$$'], ['\\[', '\\]']],
    processEscapes: true,
    macros: {
      hii: '\\mathrm{H_{II}}',
      mnras: '\\mathrm{MNRAS}',
      ion: '\\mathrm{X}'
    }
  },
  svg: {
    fontCache: 'global'
  }
};
