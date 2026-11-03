(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  root.DataMindMarkdown = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  function escapeHtml(value) {
    return String(value ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderInline(value) {
    const source = String(value ?? '');
    const parts = [];
    let cursor = 0;
    const code = /`([^`\n]+)`/g;
    let match;
    const renderText = text => escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    while ((match = code.exec(source)) !== null) {
      parts.push(renderText(source.slice(cursor, match.index)));
      parts.push(`<code>${escapeHtml(match[1])}</code>`);
      cursor = match.index + match[0].length;
    }
    parts.push(renderText(source.slice(cursor)));
    return parts.join('');
  }

  function tableCells(line) {
    let value = line.trim();
    if (value.startsWith('|')) value = value.slice(1);
    if (value.endsWith('|')) value = value.slice(0, -1);
    return value.split('|').map(cell => cell.trim());
  }

  function tableDivider(line) {
    const cells = tableCells(line);
    return cells.length > 0 && cells.every(cell => /^:?-{3,}:?$/.test(cell));
  }

  function blockStart(lines, index) {
    const line = lines[index] || '';
    return /^\s*```/.test(line) || /^#{1,6}\s+/.test(line) ||
      /^\s*([-*+] |\d+\. )/.test(line) || /^\s*>\s?/.test(line) ||
      (index + 1 < lines.length && line.includes('|') && tableDivider(lines[index + 1]));
  }

  function renderMarkdown(value) {
    const lines = String(value ?? '').replace(/\r\n?/g, '\n').split('\n');
    const output = [];
    let index = 0;

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index += 1; continue; }

      const fence = line.match(/^\s*```\s*([\w+-]*)\s*$/);
      if (fence) {
        const codeLines = [];
        index += 1;
        while (index < lines.length && !/^\s*```\s*$/.test(lines[index])) {
          codeLines.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const language = fence[1] ? ` class="language-${fence[1].toLowerCase()}"` : '';
        output.push(`<pre><code${language}>${escapeHtml(codeLines.join('\n'))}</code></pre>`);
        continue;
      }

      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        output.push(`<h${level}>${renderInline(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }

      if (index + 1 < lines.length && line.includes('|') && tableDivider(lines[index + 1])) {
        const headers = tableCells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) {
          rows.push(tableCells(lines[index]));
          index += 1;
        }
        const head = headers.map(cell => `<th>${renderInline(cell)}</th>`).join('');
        const body = rows.map(row => `<tr>${headers.map((_, cellIndex) =>
          `<td>${renderInline(row[cellIndex] || '')}</td>`).join('')}</tr>`).join('');
        output.push(`<div class="markdown-table"><table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`);
        continue;
      }

      const list = line.match(/^\s*([-*+] |\d+\. )(.*)$/);
      if (list) {
        const ordered = /\d+\. /.test(list[1]);
        const tag = ordered ? 'ol' : 'ul';
        const items = [];
        while (index < lines.length) {
          const item = lines[index].match(/^\s*([-*+] |\d+\. )(.*)$/);
          if (!item || (/\d+\. /.test(item[1]) !== ordered)) break;
          items.push(`<li>${renderInline(item[2])}</li>`);
          index += 1;
        }
        output.push(`<${tag}>${items.join('')}</${tag}>`);
        continue;
      }

      if (/^\s*>\s?/.test(line)) {
        const quotes = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
          quotes.push(lines[index].replace(/^\s*>\s?/, ''));
          index += 1;
        }
        output.push(`<blockquote>${quotes.map(renderInline).join('<br>')}</blockquote>`);
        continue;
      }

      const paragraph = [line];
      index += 1;
      while (index < lines.length && lines[index].trim() && !blockStart(lines, index)) {
        paragraph.push(lines[index]);
        index += 1;
      }
      output.push(`<p>${paragraph.map(renderInline).join('<br>')}</p>`);
    }
    return output.join('');
  }

  return { escapeHtml, renderMarkdown };
});
