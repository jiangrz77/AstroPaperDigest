# MathJax (offline copy)

The digest page loads `config.js`, followed by `tex-svg-full.js` (MathJax SVG
output). Both files are packaged into the macOS app, so formula rendering works
offline and never depends on a CDN.

Install the local offline copy:

    cd AstroPaperDigest
    mkdir -p static/mathjax
    curl -L -o static/mathjax/tex-svg-full.js \
        https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg-full.js

(~2 MB, single file.) Rebuild the app after updating the local copy.
