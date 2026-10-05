// Lesson plan outputs, built in the browser: Copy for Compass (rich HTML on the clipboard) and a Word document.
// Both happen in the page so they work on the school server's plain http:// address (the copy has to happen in
// the click itself) and in the online demo, which has no server. The maths conversion and the Compass HTML are
// ported from LP-Generator, whose output is known to paste cleanly into Compass.
"use strict";

// \$ is a literal dollar sign in plan text; a bare $ starts maths. Swapped out while maths is found.
const DOLLAR = "\u0001";
const protectDollars = s => String(s ?? "").replace(/\\\$/g, DOLLAR);
const restoreDollars = s => s.replace(/\u0001/g, () => "$");

// ---------------------------------------------------------------- LaTeX → Unicode (LP-Generator's latexToUnicode)
// stacked: write simple fractions as ⁵⁄₉. Off for Word, where those come out too small to read.
function latexToUnicode(s, stacked = true) {
  const GREEK = {
    "\\alpha": "α", "\\beta": "β", "\\gamma": "γ", "\\delta": "δ", "\\epsilon": "ε", "\\varepsilon": "ε", "\\zeta": "ζ",
    "\\eta": "η", "\\theta": "θ", "\\iota": "ι", "\\kappa": "κ", "\\lambda": "λ", "\\mu": "μ", "\\nu": "ν", "\\xi": "ξ",
    "\\pi": "π", "\\rho": "ρ", "\\sigma": "σ", "\\tau": "τ", "\\upsilon": "υ", "\\phi": "φ", "\\varphi": "φ", "\\chi": "χ",
    "\\psi": "ψ", "\\omega": "ω", "\\Gamma": "Γ", "\\Delta": "Δ", "\\Theta": "Θ", "\\Lambda": "Λ", "\\Xi": "Ξ", "\\Pi": "Π",
    "\\Sigma": "Σ", "\\Upsilon": "Υ", "\\Phi": "Φ", "\\Psi": "Ψ", "\\Omega": "Ω",
  };
  // \left and \right only resize the delimiter after them, but must be matched in the same longest-first pass as
  // \leftarrow, \le and friends: replaced separately, one mangles the other.
  const OPS = {
    "\\left": "", "\\right": "",
    "\\times": "×", "\\div": "÷", "\\pm": "±", "\\mp": "∓", "\\cdot": "·", "\\cdots": "⋯", "\\ldots": "…",
    "\\leq": "≤", "\\le": "≤", "\\geq": "≥", "\\ge": "≥", "\\neq": "≠", "\\ne": "≠",
    "\\approx": "≈", "\\equiv": "≡", "\\sim": "∼", "\\propto": "∝",
    "\\infty": "∞", "\\partial": "∂", "\\nabla": "∇", "\\sum": "Σ", "\\prod": "Π", "\\int": "∫",
    "\\in": "∈", "\\notin": "∉", "\\subset": "⊂", "\\subseteq": "⊆", "\\cup": "∪", "\\cap": "∩",
    "\\emptyset": "∅", "\\forall": "∀", "\\exists": "∃",
    "\\rightarrow": "→", "\\to": "→", "\\leftarrow": "←",
    "\\Rightarrow": "⇒", "\\Leftarrow": "⇐", "\\Leftrightarrow": "⟺", "\\leftrightarrow": "↔",
    "\\iff": "⟺", "\\implies": "⟹", "\\impliedby": "⟸", "\\mapsto": "↦",
    "\\rightleftharpoons": "⇌", "\\leftrightharpoons": "⇋",
    "\\therefore": "∴", "\\because": "∵", "\\degree": "°", "\\circ": "°",
    "\\angle": "∠", "\\measuredangle": "∡", "\\triangle": "△", "\\square": "□",
    "\\cong": "≅", "\\ncong": "≇", "\\simeq": "≃", "\\nsim": "≁",
    "\\perp": "⊥", "\\parallel": "∥", "\\nparallel": "∦",
    "\\langle": "⟨", "\\rangle": "⟩", "\\Vert": "‖", "\\vert": "|", "\\mid": "|",
    "\\vdots": "⋮", "\\ddots": "⋱", "\\iddots": "⋰",
    "\\oplus": "⊕", "\\otimes": "⊗", "\\odot": "⊙", "\\bullet": "•", "\\star": "★", "\\checkmark": "✓",
  };
  const SUP = { 0: "⁰", 1: "¹", 2: "²", 3: "³", 4: "⁴", 5: "⁵", 6: "⁶", 7: "⁷", 8: "⁸", 9: "⁹", "+": "⁺", "-": "⁻",
    "=": "⁼", "(": "⁽", ")": "⁾", a: "ᵃ", b: "ᵇ", c: "ᶜ", d: "ᵈ", e: "ᵉ", f: "ᶠ", g: "ᵍ", h: "ʰ", i: "ⁱ", j: "ʲ", k: "ᵏ",
    l: "ˡ", m: "ᵐ", n: "ⁿ", o: "ᵒ", p: "ᵖ", r: "ʳ", s: "ˢ", t: "ᵗ", u: "ᵘ", v: "ᵛ", w: "ʷ", x: "ˣ", y: "ʸ", z: "ᶻ" };
  const SUB = { 0: "₀", 1: "₁", 2: "₂", 3: "₃", 4: "₄", 5: "₅", 6: "₆", 7: "₇", 8: "₈", 9: "₉", "+": "₊", "-": "₋",
    "=": "₌", "(": "₍", ")": "₎", a: "ₐ", e: "ₑ", h: "ₕ", i: "ᵢ", j: "ⱼ", k: "ₖ", l: "ₗ", m: "ₘ", n: "ₙ", o: "ₒ", p: "ₚ",
    r: "ᵣ", s: "ₛ", t: "ₜ", u: "ᵤ", v: "ᵥ", x: "ₓ" };
  const VULGAR = { "1/2": "½", "1/3": "⅓", "2/3": "⅔", "1/4": "¼", "3/4": "¾", "1/5": "⅕", "2/5": "⅖", "3/5": "⅗",
    "4/5": "⅘", "1/6": "⅙", "5/6": "⅚", "1/7": "⅐", "1/8": "⅛", "3/8": "⅜", "5/8": "⅝", "7/8": "⅞", "1/9": "⅑", "1/10": "⅒" };
  const SYMBOLS = { ...GREEK, ...OPS };
  const escapeRe = str => str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const symbolRe = new RegExp(Object.keys(SYMBOLS).sort((a, b) => b.length - a.length).map(escapeRe).join("|") + "(?![A-Za-z])", "g");
  s = s.replace(symbolRe, m => SYMBOLS[m]);
  s = s.replace(/\\\\/g, " ");
  s = s.replace(/\\[,;:]/g, " ").replace(/\\!/g, "");
  s = s.replace(/\\qquad/g, "   ").replace(/\\quad/g, "  ");
  s = s.replace(/\\text\{([^}]*)\}/g, "$1");
  const MATHBB = { R: "ℝ", Z: "ℤ", N: "ℕ", Q: "ℚ", C: "ℂ" };
  s = s.replace(/\\mathbb\{([^}]*)\}/g, (_, c) => MATHBB[c] || c);
  s = s.replace(/\\(?:mathbf|mathrm|mathit|mathcal|boldsymbol|overrightarrow|underline|widehat|widetilde|underbrace|overbrace)\{([^}]*)\}/g, "$1");
  s = s.replace(/\\(?:bar|overline)\{([^}]*)\}/g, (_, c) => c + "\u0304");
  s = s.replace(/\\hat\{([^}]*)\}/g, (_, c) => c + "\u0302");
  s = s.replace(/\\tilde\{([^}]*)\}/g, (_, c) => c + "\u0303");
  s = s.replace(/\\vec\{([^}]*)\}/g, (_, c) => c + "\u20D7");
  s = s.replace(/\\dot\{([^}]*)\}/g, (_, c) => c + "\u0307");
  s = s.replace(/\\ddot\{([^}]*)\}/g, (_, c) => c + "\u0308");
  s = s.replace(/\\sqrt\[([^\]]+)\]\{([^}]*)\}/g, "$1√($2)");
  s = s.replace(/\\d?frac\{([^}]*)\}\{([^}]*)\}/g, (_, num, den) => {
    const n = num.trim(), d = den.trim();
    if (VULGAR[n + "/" + d]) return VULGAR[n + "/" + d];
    const simple = x => /^[A-Za-z0-9]+$/.test(x);
    if (stacked && simple(n) && simple(d) && [...n].every(c => SUP[c] !== undefined) && [...d].every(c => SUB[c] !== undefined))
      return [...n].map(c => SUP[c]).join("") + "⁄" + [...d].map(c => SUB[c]).join("");
    const wrap = x => /[\s+\-×·/=]/.test(x) ? "(" + x + ")" : x;
    return wrap(n) + "/" + wrap(d);
  });
  s = s.replace(/\\sqrt\{([^}]*)\}/g, "√($1)");
  s = s.replace(/\^\{([^}]+)\}/g, (_, m) => [...m].every(c => SUP[c] !== undefined) ? [...m].map(c => SUP[c]).join("") : "^(" + m + ")");
  s = s.replace(/\^(\w)/g, (_, c) => SUP[c] !== undefined ? SUP[c] : "^" + c);
  s = s.replace(/_\{([^}]+)\}/g, (_, m) => [...m].every(c => SUB[c] !== undefined) ? [...m].map(c => SUB[c]).join("") : "_(" + m + ")");
  s = s.replace(/_(\w)/g, (_, c) => SUB[c] !== undefined ? SUB[c] : "_" + c);
  s = s.replace(/\{([^}]*)\}/g, "$1");
  s = s.replace(/\\([A-Za-z]+)/g, "$1");
  return s;
}

// Maths that reads correctly as plain text (x² + 3x). Fractions, roots and the like need a real equation.
function unicodeSafe(m) {
  if (/\\d?frac|\\over|\\sqrt|\\begin|\\int|\\sum|\\prod|\\lim|\\binom/.test(m)) return false;
  const u = latexToUnicode(m);
  return !/[\\{}]/.test(u) && !/[\^_]/.test(u);
}

// In the L section every line but the three headings is a dot point (as LP-Generator pastes it).
function bulletLSection(text) {
  const HEADING = /^\*{0,2}(Learning Intentions?|Success Criteria|Do Now)\*{0,2}:?\s*$/i;
  return String(text || "").split("\n").map(line => {
    const t = line.trim();
    if (!t || HEADING.test(t) || /^([-•*]|\d+\.\s)/.test(t)) return line;
    return "- " + t;
  }).join("\n");
}
const sectionText = (k, text) => k === "L" ? bulletLSection(text) : String(text || "");

// ---------------------------------------------------------------- Copy for Compass
function compassHtml(text) {
  if (!String(text || "").trim()) return "";
  let h = protectDollars(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  // Compass's equation widget (CKEditor MathJax, class "my-math") can't sit inline without overlapping the line,
  // so it always gets its own line; maths that reads fine as text stays text.
  const block = m => { m = m.trim(); return m ? '<br><span class="my-math">\\[' + m + "\\]</span><br>" : ""; };
  h = h.replace(/[ \t]*\n?[ \t]*\$\$([^$]+)\$\$[ \t]*\n?[ \t]*/g, (_, m) => unicodeSafe(m) ? "<br>" + latexToUnicode(m.trim()) + "<br>" : block(m));
  h = h.replace(/ *\$([^$\n]+)\$ */g, (_, m) => unicodeSafe(m) ? " " + latexToUnicode(m.trim()) + " " : block(m));
  h = h.replace(/([^\s>]) +([.,;:!?])(?![.,;:!?])/g, "$1$2");
  h = h.replace(/!\[([^\]\n]*)\]\(([^)\s]+)\)/g, (_, alt, src) => /^https?:\/\//.test(src)
    ? `<br><img src="${src}" alt="${alt}" style="max-width:100%;max-height:320px;"><br>`
    : `<br><i>[image: ${alt || "see the lesson plan"}]</i><br>`);
  h = h.replace(/\[([^\]\n]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2">$1</a>');
  h = h.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/\*\*/g, "");
  h = h.replace(/\*([^*\n]+)\*/g, "<i>$1</i>");
  h = h.replace(/^(\d+)\.\s(.+)$/gm, '<span style="display:block;padding-left:20px;"><b>$1.</b>&nbsp;$2</span>');
  h = h.replace(/^-\s(.+)$/gm, '<span style="display:block;padding-left:20px;text-indent:-12px;">&bull;&nbsp;$1</span>');
  // A bullet or numbered line is already a line of its own: a <br> after it would add a blank line.
  h = h.replace(/(<span style="display:block;[^"]*">.*<\/span>)\n/g, "$1");
  h = h.replace(/\n/g, "<br>");
  return restoreDollars(h);
}

function plainText(text) {
  let t = protectDollars(text);
  t = t.replace(/\$\$([^$]+)\$\$/g, (_, m) => latexToUnicode(m)).replace(/\$([^$\n]+)\$/g, (_, m) => latexToUnicode(m));
  t = t.replace(/\*\*(.+?)\*\*/g, "$1").replace(/\*([^*\n]+)\*/g, "$1");
  return restoreDollars(t);
}

// logo: {dataUrl, width, height} or null; schoolName is shown instead when there is no logo.
function buildCompassClipboard(plan, sections, logo, schoolName) {
  const CELL = "padding:6px 10px;vertical-align:top;border:1px solid #CCC;";
  const esc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const logoCell = logo ? `<img src="${logo.dataUrl}" style="height:60px;width:auto;" alt="${esc(schoolName)}">`
    : `<span style="font-size:16px;font-weight:700;color:#8B0000;">${esc(schoolName.toUpperCase())}</span>`;
  const rows = sections.map(([k, name]) => `<tr>
    <td style="${CELL}font-size:20px;font-weight:700;text-align:center;width:30px;min-width:30px;max-width:30px;color:#333;">${k}</td>
    <td style="${CELL}font-weight:700;font-size:12px;color:#444;width:95px;min-width:95px;max-width:95px;">${name}</td>
    <td style="${CELL}font-size:13px;line-height:1.55;">${compassHtml(sectionText(k, plan.sections[k]))}</td></tr>`).join("");
  // A paragraph either side: flush against the field's edges, Compass has nowhere to put the cursor and clips the
  // last row. &nbsp; because the editor drops empty paragraphs.
  const SPACER = "<p>&nbsp;</p>";
  const html = SPACER + `<table style="border-collapse:collapse;font-family:Segoe UI,Arial,sans-serif;font-size:13px;width:100%">` +
    `<tr><td colspan="3" style="${CELL}">${logoCell}</td></tr>${rows}</table>` + SPACER;
  const plain = sections.map(([k, name]) => `${name}\n${"-".repeat(name.length)}\n${plainText(sectionText(k, plan.sections[k]))}\n`).join("\n");
  return { html, plain };
}

// True only if the copy really happened. On plain http the clipboard API is unavailable, so the copy event is
// used; that works only inside the click itself, which is why nothing here waits on the network.
async function copyRich(html, plain) {
  if (navigator.clipboard && window.ClipboardItem && window.isSecureContext) {
    try {
      await navigator.clipboard.write([new ClipboardItem({
        "text/plain": new Blob([plain], { type: "text/plain" }), "text/html": new Blob([html], { type: "text/html" }) })]);
      return true;
    } catch {}
  }
  let ok = false;
  const handler = e => { e.clipboardData.setData("text/html", html); e.clipboardData.setData("text/plain", plain); e.preventDefault(); ok = true; };
  document.addEventListener("copy", handler);
  try { document.execCommand("copy"); } catch {}
  document.removeEventListener("copy", handler);
  return ok;
}

// ---------------------------------------------------------------- Word (.docx)
const xmlEsc = s => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// One line of plan text → Word runs: maths as Unicode text, **bold**, *italic*, links as their label.
function docxRuns(line, base = "") {
  let t = protectDollars(line);
  t = t.replace(/\$\$([^$]+)\$\$/g, (_, m) => latexToUnicode(m.trim(), false)).replace(/\$([^$\n]+)\$/g, (_, m) => latexToUnicode(m.trim(), false));
  t = t.replace(/!\[([^\]\n]*)\]\([^)\s]+\)/g, (_, alt) => `[image: ${alt || "see the lesson plan"}]`);
  t = t.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, "$1 ($2)");
  const runs = [];
  for (const part of restoreDollars(t).split(/(\*\*[^*]+\*\*|\*[^*\s][^*]*\*)/)) {
    if (!part) continue;
    const bold = part.startsWith("**") && part.endsWith("**") && part.length > 4;
    const italic = !bold && part.startsWith("*") && part.endsWith("*") && part.length > 2;
    const text = bold ? part.slice(2, -2) : italic ? part.slice(1, -1) : part;
    runs.push(`<w:r><w:rPr>${base}${bold ? "<w:b/>" : ""}${italic ? "<w:i/>" : ""}</w:rPr><w:t xml:space="preserve">${xmlEsc(text)}</w:t></w:r>`);
  }
  return runs.join("");
}

function docxParagraphs(text) {
  const out = [];
  for (const line of String(text || "").split("\n")) {
    if (!line.trim()) continue;
    const bullet = line.match(/^-\s+(.*)$/), numbered = line.match(/^(\d+)\.\s+(.*)$/);
    const display = /^\s*\$\$[^$]+\$\$\s*$/.test(protectDollars(line));
    const ind = bullet || numbered ? '<w:ind w:left="284" w:hanging="227"/>' : "";
    const jc = display ? '<w:jc w:val="center"/>' : "";
    const lead = bullet ? `<w:r><w:t xml:space="preserve">•\u00a0</w:t></w:r>`
      : numbered ? `<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">${numbered[1]}.\u00a0</w:t></w:r>` : "";
    out.push(`<w:p><w:pPr><w:spacing w:after="40"/>${ind}${jc}</w:pPr>${lead}${docxRuns(bullet ? bullet[1] : numbered ? numbered[2] : line)}</w:p>`);
  }
  return out.join("") || "<w:p/>";
}

function docxDocument(plan, sections, logo, schoolName, title) {
  const W = [700, 1700, 7506];  // twips: A4 less 1000-twip margins
  const border = '<w:top w:val="single" w:sz="4" w:color="CCCCCC"/><w:left w:val="single" w:sz="4" w:color="CCCCCC"/>' +
    '<w:bottom w:val="single" w:sz="4" w:color="CCCCCC"/><w:right w:val="single" w:sz="4" w:color="CCCCCC"/>';
  const cell = (w, body, extra = "") => `<w:tc><w:tcPr><w:tcW w:w="${w}" w:type="dxa"/>${extra}</w:tcPr>${body}</w:tc>`;
  let logoBody;
  if (logo) {
    const cy = 540000, cx = Math.round(cy * logo.width / logo.height);  // 1.5 cm high
    logoBody = `<w:p><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="${cx}" cy="${cy}"/>` +
      `<wp:docPr id="1" name="Logo"/><a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">` +
      `<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">` +
      `<pic:nvPicPr><pic:cNvPr id="1" name="logo.png"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="rIdLogo"/>` +
      `<a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="${cx}" cy="${cy}"/></a:xfrm>` +
      `<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>`;
  } else {
    logoBody = `<w:p><w:r><w:rPr><w:b/><w:color w:val="8B0000"/><w:sz w:val="32"/></w:rPr><w:t>${xmlEsc(schoolName.toUpperCase())}</w:t></w:r></w:p>`;
  }
  const rows = [`<w:tr>${cell(W[0] + W[1] + W[2], logoBody, '<w:gridSpan w:val="3"/>')}</w:tr>`];
  for (const [k, name] of sections) {
    rows.push(`<w:tr><w:trPr><w:cantSplit/></w:trPr>` +
      cell(W[0], `<w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/><w:color w:val="333333"/><w:sz w:val="40"/></w:rPr><w:t>${k}</w:t></w:r></w:p>`) +
      cell(W[1], `<w:p><w:r><w:rPr><w:b/><w:color w:val="444444"/><w:sz w:val="20"/></w:rPr><w:t>${xmlEsc(name)}</w:t></w:r></w:p>`) +
      cell(W[2], docxParagraphs(sectionText(k, plan.sections[k]))) + "</w:tr>");
  }
  const when = plan.lesson_date ? new Date(plan.lesson_date + "T00:00").toLocaleDateString("en-AU", { weekday: "long", day: "numeric", month: "long", year: "numeric" }) : "";
  const sub = [plan.subject, plan.year_level && `Year ${plan.year_level}`, when].filter(Boolean).join(" · ");
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"><w:body>` +
    `<w:p><w:pPr><w:spacing w:after="0"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="28"/></w:rPr><w:t>${xmlEsc(title)}</w:t></w:r></w:p>` +
    (sub ? `<w:p><w:r><w:rPr><w:color w:val="555555"/></w:rPr><w:t>${xmlEsc(sub)}</w:t></w:r></w:p>` : "") +
    `<w:tbl><w:tblPr><w:tblW w:w="${W[0] + W[1] + W[2]}" w:type="dxa"/><w:tblLayout w:type="fixed"/><w:tblBorders>${border}` +
    `<w:insideH w:val="single" w:sz="4" w:color="CCCCCC"/><w:insideV w:val="single" w:sz="4" w:color="CCCCCC"/></w:tblBorders>` +
    `<w:tblCellMar><w:top w:w="80" w:type="dxa"/><w:left w:w="140" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/><w:right w:w="140" w:type="dxa"/></w:tblCellMar></w:tblPr>` +
    `<w:tblGrid>${W.map(w => `<w:gridCol w:w="${w}"/>`).join("")}</w:tblGrid>${rows.join("")}</w:tbl>` +
    `<w:p/><w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1000" w:right="1000" w:bottom="1000" w:left="1000" w:header="500" w:footer="500" w:gutter="0"/></w:sectPr></w:body></w:document>`;
}

const DOCX_STYLES = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr>` +
  `<w:rFonts w:ascii="Calibri" w:hAnsi="Calibri" w:eastAsia="Calibri" w:cs="Calibri"/><w:sz w:val="22"/><w:szCs w:val="22"/><w:lang w:val="en-AU"/>` +
  `</w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="80" w:line="259" w:lineRule="auto"/></w:pPr></w:pPrDefault></w:docDefaults>` +
  `<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style></w:styles>`;

// A .docx is a zip. Stored (uncompressed) entries are all Word needs, so no library is required.
const CRC_TABLE = Array.from({ length: 256 }, (_, n) => { let c = n; for (let k = 0; k < 8; k++) c = c & 1 ? 0xEDB88320 ^ (c >>> 1) : c >>> 1; return c >>> 0; });
function crc32(bytes) { let c = 0xFFFFFFFF; for (const b of bytes) c = CRC_TABLE[(c ^ b) & 0xFF] ^ (c >>> 8); return (c ^ 0xFFFFFFFF) >>> 0; }
function zip(files) {  // [{name, data: Uint8Array}]
  const enc = new TextEncoder(), parts = [], central = [];
  let offset = 0;
  for (const f of files) {
    const name = enc.encode(f.name), crc = crc32(f.data), size = f.data.length;
    const head = (sig, extra) => { const b = new DataView(new ArrayBuffer(extra)); b.setUint32(0, sig, true); return b; };
    const local = head(0x04034b50, 30);
    [[4, 20], [6, 0x0800], [8, 0], [10, 0], [12, 0x21]].forEach(([o, v]) => local.setUint16(o, v, true));
    local.setUint32(14, crc, true); local.setUint32(18, size, true); local.setUint32(22, size, true); local.setUint16(26, name.length, true);
    const cen = head(0x02014b50, 46);
    [[4, 20], [6, 20], [8, 0x0800], [10, 0], [12, 0], [14, 0x21]].forEach(([o, v]) => cen.setUint16(o, v, true));
    cen.setUint32(16, crc, true); cen.setUint32(20, size, true); cen.setUint32(24, size, true); cen.setUint16(28, name.length, true);
    cen.setUint32(42, offset, true);
    parts.push(new Uint8Array(local.buffer), name, f.data);
    central.push(new Uint8Array(cen.buffer), name);
    offset += 30 + name.length + size;
  }
  const cenSize = central.reduce((n, p) => n + p.length, 0);
  const end = new DataView(new ArrayBuffer(22));
  end.setUint32(0, 0x06054b50, true); end.setUint16(8, files.length, true); end.setUint16(10, files.length, true);
  end.setUint32(12, cenSize, true); end.setUint32(16, offset, true);
  return new Blob([...parts, ...central, new Uint8Array(end.buffer)], { type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document" });
}

function buildDocx(plan, sections, logo, schoolName, title) {
  const enc = new TextEncoder();
  const files = [
    { name: "[Content_Types].xml", data: enc.encode(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>` +
      `<Default Extension="xml" ContentType="application/xml"/><Default Extension="png" ContentType="image/png"/>` +
      `<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>` +
      `<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>`) },
    { name: "_rels/.rels", data: enc.encode(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>`) },
    { name: "word/_rels/document.xml.rels", data: enc.encode(`<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>` +
      (logo ? `<Relationship Id="rIdLogo" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/logo.png"/>` : "") + "</Relationships>") },
    { name: "word/styles.xml", data: enc.encode(DOCX_STYLES) },
    { name: "word/document.xml", data: enc.encode(docxDocument(plan, sections, logo, schoolName, title)) },
  ];
  if (logo) files.push({ name: "word/media/logo.png", data: logo.png });
  return zip(files);
}

// The school logo as PNG (Word can't show WebP), loaded once when the page opens so copying never waits.
async function loadLogo(url) {
  try {
    const img = new Image();
    img.src = url;
    await img.decode();
    const h = Math.min(img.naturalHeight, 160), w = Math.round(img.naturalWidth * h / img.naturalHeight);
    const canvas = Object.assign(document.createElement("canvas"), { width: w, height: h });
    canvas.getContext("2d").drawImage(img, 0, 0, w, h);
    const dataUrl = canvas.toDataURL("image/png");
    const png = Uint8Array.from(atob(dataUrl.split(",")[1]), c => c.charCodeAt(0));
    return { dataUrl, png, width: w, height: h };
  } catch { return null; }
}
