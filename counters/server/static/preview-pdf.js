/* The scrolling PDF preview, rendered with pdf.js.
 *
 * A PDF cannot be shown the way the other media kinds are. Every one of them
 * hands the bytes to a native element (<img>, <audio>, <video>) inside a
 * script-free wrapper, but the browser's own PDF viewer is a *plugin*, and the
 * sandbox flags that confine these frames block plugins outright — an
 * <iframe src=/content/n> pointing at a PDF renders as a broken-document icon
 * and nothing else. So this is the third scripted preview, alongside raw
 * HTML/SVG and the model viewer: pdf.js decodes the file and paints each page
 * into a <canvas>, which is ordinary DOM the sandbox is happy with.
 *
 * The document is never trusted. pdf.js does not run a PDF's own JavaScript and
 * `isEvalSupported` is off, so the file is only ever *data* to a parser. That
 * is what lets this frame keep a real origin (`allow-scripts allow-same-origin`)
 * where a raw HTML or SVG inscription — which really is executable content —
 * must stay on the opaque one. The distinction is worth stating plainly,
 * because an opaque origin is not merely stricter here, it is unusable: a
 * document without `allow-same-origin` cannot start a worker (the URL is
 * cross-origin to it) and cannot load an ES module at all — the entry script
 * arrives but its imports are never fetched, and a dynamic import() hangs
 * rather than rejecting. pdf.js falls back to decoding on the main thread when
 * it cannot have a worker, and that fallback paints nothing at all.
 *
 * pdf.js is vendored rather than pulled from a CDN, so the explorer renders the
 * same offline as online and the bytes are pinned in-tree. It is the *classic*
 * build, loaded with a plain <script>, which is why it is held to the 3.x line:
 * 4.x ships ES modules only.
 *
 * Counters run to entire blocks, so the interesting PDFs are *long*. Two things
 * follow, and both matter more than they would for a two-page file:
 *
 *   * Pages render only as they approach the viewport, and are released once
 *     they are well behind it. A 400-page book at full-width canvases would
 *     otherwise cost hundreds of megabytes of bitmap.
 *   * `disableAutoFetch` keeps pdf.js from pulling the whole file up front; it
 *     asks for byte ranges as it needs them (the server answers 206). A card
 *     thumbnail showing page 1 costs a few KB, not the whole inscription.
 */

(function () {
  'use strict';

  var WORKER = '/pdfjs.worker.min.js';
  var FONTS = '/pdfjs-standard-fonts/';
  // How far outside the viewport a page is still worth having painted, as a
  // percentage of the frame height. One screen either way keeps scrolling ahead
  // of the renderer without holding much more bitmap than the screen itself.
  var NEAR = '100%';
  // Cap the backing store: past this the extra pixels are invisible but the
  // memory is real. 2 covers every ordinary HiDPI display.
  var MAX_DPR = 2;

  var root = document.getElementById('pdfdoc');
  var status = document.getElementById('pdfstatus');
  var counter = document.getElementById('pdfpage');

  /** Swap the viewer out for a plain message and a download link. Any failure
   *  here (a damaged file, a PDF pdf.js refuses) has to leave the bytes
   *  reachable — an inscription is permanent, our renderer is not. */
  function fallback(message) {
    root.textContent = '';
    var box = document.createElement('div');
    box.className = 'pdffail';
    var p = document.createElement('p');
    p.textContent = message;
    var a = document.createElement('a');
    a.href = root.dataset.src;
    a.download = 'counter-' + root.dataset.number + '.pdf';
    a.textContent = 'Download the PDF';
    box.appendChild(p);
    box.appendChild(a);
    root.appendChild(box);
    if (status) status.remove();
    if (counter) counter.remove();
  }

  function main() {
    var pdfjsLib = window.pdfjsLib;
    if (!pdfjsLib) return fallback('The PDF viewer could not be loaded.');

    pdfjsLib.GlobalWorkerOptions.workerSrc = WORKER;
    pdfjsLib.getDocument({
      url: root.dataset.src,
      disableAutoFetch: true,     // byte ranges on demand, not the whole file
      isEvalSupported: false,     // no eval here, and none wanted
      standardFontDataUrl: FONTS, // for PDFs leaning on the standard 14 fonts
    }).promise.then(show).catch(function (err) {
      console.error('pdf preview failed', err);
      fallback('This file could not be read as a PDF.');
    });
  }

  function show(doc) {
    if (status) status.remove();

    // Page 1 sizes every placeholder, so the scrollbar is honest before a
    // single page has painted and nothing jumps as they arrive. Pages that turn
    // out to be a different shape correct themselves when they render.
    doc.getPage(1).then(function (first) {
      var unit = first.getViewport({ scale: 1 });
      var holders = [];
      var tasks = {};                     // page number -> in-flight RenderTask

      for (var n = 1; n <= doc.numPages; n++) {
        var holder = document.createElement('div');
        holder.className = 'pdfpage';
        holder.style.aspectRatio = unit.width + ' / ' + unit.height;
        holder.dataset.page = String(n);
        root.appendChild(holder);
        holders.push(holder);
      }
      if (counter && doc.numPages > 1) counter.textContent = '1 / ' + doc.numPages;

      function paint(holder) {
        var n = Number(holder.dataset.page);
        if (tasks[n] || holder.firstChild) return;
        var width = holder.clientWidth;
        if (!width) return;    // laid out at zero (hidden frame): nothing to draw

        doc.getPage(n).then(function (page) {
          if (holder.firstChild) return;
          var dpr = Math.min(window.devicePixelRatio || 1, MAX_DPR);
          var viewport = page.getViewport({ scale: (width / unit.width) * dpr });
          var canvas = document.createElement('canvas');
          canvas.width = Math.floor(viewport.width);
          canvas.height = Math.floor(viewport.height);
          // Correct the placeholder to this page's true shape.
          holder.style.aspectRatio = viewport.width + ' / ' + viewport.height;

          var task = page.render({ canvasContext: canvas.getContext('2d'), viewport: viewport });
          tasks[n] = task;
          task.promise.then(function () {
            holder.replaceChildren(canvas);
          }).catch(function () {
            /* cancelled by release(), or a page that will not draw: leave blank */
          }).then(function () {
            delete tasks[n];
            page.cleanup();
          });
        }).catch(function () { /* page unreadable: leave the placeholder */ });
      }

      function release(holder) {
        var n = Number(holder.dataset.page);
        if (tasks[n]) { tasks[n].cancel(); delete tasks[n]; }
        holder.replaceChildren();
      }

      // Which pages are on (or near) screen. Kept as a set rather than derived
      // on demand, because the resize path below has to know what to repaint —
      // including pages that never painted at all. A frame is often still
      // sizing when the first intersections fire, and `paint` declines to draw
      // into zero width; without this they would stay blank for good.
      var visible = new Set();
      var observer = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) { visible.add(entry.target); paint(entry.target); }
          else { visible.delete(entry.target); release(entry.target); }
        });
      }, { rootMargin: NEAR + ' 0px' });
      holders.forEach(function (h) { observer.observe(h); });

      // Which page you are on: the topmost one still touching the viewport.
      if (counter && doc.numPages > 1) {
        var spy = new IntersectionObserver(function (entries) {
          entries.forEach(function (entry) {
            if (!entry.isIntersecting) return;
            counter.textContent = entry.target.dataset.page + ' / ' + doc.numPages;
          });
        }, { rootMargin: '0px 0px -85% 0px' });
        holders.forEach(function (h) { spy.observe(h); });
      }

      // Canvases are sized to the width they were painted at, so a resized
      // frame has to repaint what is on screen — and paint, for the first time,
      // anything that was on screen while the frame still had no width.
      // Debounced: a drag fires continuously.
      var pending;
      new ResizeObserver(function () {
        clearTimeout(pending);
        pending = setTimeout(function () {
          visible.forEach(function (holder) {
            release(holder);
            paint(holder);
          });
        }, 150);
      }).observe(root);
    }).catch(function () {
      fallback('This PDF has no readable pages.');
    });
  }

  main();
})();
