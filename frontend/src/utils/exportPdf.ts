/**
 * exportPdf — render a DOM element to a multi-page A4 PDF and trigger a download.
 *
 * Used by the Executive Report "Download PDF" action. The element passed in is a
 * dedicated, always-light "PDF document" rendered off-screen (see
 * ExecutiveReportPdfDocument) — never the live page chrome — so the result is a
 * clean, board-ready report with no navbar or buttons.
 *
 * html2canvas + jsPDF are imported dynamically so they stay out of the initial
 * bundle and only load when a user actually exports.
 *
 * Pagination: a single tall raster of the element is sliced into A4 pages. Slice
 * boundaries prefer any `[data-pdf-break]` marker that fits on the current page,
 * so sections are never cut mid-card. If a section is taller than a page, the
 * slicer falls back to a hard cut to guarantee progress.
 */

export interface PdfExportOptions {
  /** Download filename, e.g. "AgentIQ-Executive-Report.pdf". */
  filename: string;
  /** Left-aligned footer text drawn on every page (page numbers are appended). */
  footerText?: string;
  /** Raster scale. Higher = crisper but larger file. Default 2. */
  scale?: number;
  /** CSS selector for allowed page-break points within the node. */
  breakSelector?: string;
}

// A4 portrait, millimetres.
const PAGE_W_MM = 210;
const PAGE_H_MM = 297;
const MARGIN_X_MM = 12;
const MARGIN_TOP_MM = 14;
const MARGIN_BOTTOM_MM = 16;
const CONTENT_W_MM = PAGE_W_MM - MARGIN_X_MM * 2;
const CONTENT_H_MM = PAGE_H_MM - MARGIN_TOP_MM - MARGIN_BOTTOM_MM;

export async function downloadElementAsPdf(
  node: HTMLElement,
  options: PdfExportOptions,
): Promise<void> {
  const {
    filename,
    footerText,
    scale = 2,
    breakSelector = '[data-pdf-break]',
  } = options;

  // Lazy-load the heavy deps only when an export is actually requested.
  const [{ default: html2canvas }, { jsPDF }] = await Promise.all([
    import('html2canvas'),
    import('jspdf'),
  ]);

  const canvas = await html2canvas(node, {
    scale,
    backgroundColor: '#ffffff',
    useCORS: true,
    logging: false,
    // Capture the element at its natural laid-out size.
    windowWidth: node.scrollWidth,
    windowHeight: node.scrollHeight,
  });

  const pxPerMm = canvas.width / CONTENT_W_MM;
  const pageContentHeightPx = CONTENT_H_MM * pxPerMm;

  // Allowed break y-positions (canvas px), relative to the captured node.
  const nodeTop = node.getBoundingClientRect().top;
  const markerYs = Array.from(node.querySelectorAll<HTMLElement>(breakSelector))
    .map((el) => (el.getBoundingClientRect().top - nodeTop) * scale)
    .filter((y) => y > 1 && y < canvas.height)
    .sort((a, b) => a - b);
  const breakCandidates = [...markerYs, canvas.height];

  // Greedily fill each page up to pageContentHeightPx, snapping to the furthest
  // allowed break that fits; hard-cut only when a single block overflows a page.
  const slices: Array<{ start: number; end: number }> = [];
  let start = 0;
  // Guard against pathological loops (e.g. zero-height canvas).
  let safety = 0;
  while (start < canvas.height - 1 && safety < 1000) {
    safety += 1;
    const maxEnd = start + pageContentHeightPx;
    if (maxEnd >= canvas.height) {
      slices.push({ start, end: canvas.height });
      break;
    }
    const fitting = breakCandidates.filter((b) => b > start + 1 && b <= maxEnd);
    const end = fitting.length ? Math.max(...fitting) : maxEnd;
    slices.push({ start, end });
    start = end;
  }
  if (slices.length === 0) slices.push({ start: 0, end: canvas.height });

  const pdf = new jsPDF({ unit: 'mm', format: 'a4', orientation: 'portrait' });

  slices.forEach((slice, i) => {
    if (i > 0) pdf.addPage();

    const sliceHeightPx = slice.end - slice.start;
    const pageCanvas = document.createElement('canvas');
    pageCanvas.width = canvas.width;
    pageCanvas.height = sliceHeightPx;
    const ctx = pageCanvas.getContext('2d');
    if (ctx) {
      ctx.fillStyle = '#ffffff';
      ctx.fillRect(0, 0, pageCanvas.width, pageCanvas.height);
      ctx.drawImage(
        canvas,
        0,
        slice.start,
        canvas.width,
        sliceHeightPx,
        0,
        0,
        canvas.width,
        sliceHeightPx,
      );
    }

    const imgData = pageCanvas.toDataURL('image/png');
    const sliceHeightMm = sliceHeightPx / pxPerMm;
    pdf.addImage(imgData, 'PNG', MARGIN_X_MM, MARGIN_TOP_MM, CONTENT_W_MM, sliceHeightMm);

    // Footer: left = caption, right = page numbers.
    pdf.setFont('helvetica', 'normal');
    pdf.setFontSize(8);
    pdf.setTextColor(120, 130, 150);
    if (footerText) {
      pdf.text(footerText, MARGIN_X_MM, PAGE_H_MM - 8);
    }
    pdf.text(`Page ${i + 1} of ${slices.length}`, PAGE_W_MM - MARGIN_X_MM, PAGE_H_MM - 8, {
      align: 'right',
    });
  });

  pdf.save(filename);
}
