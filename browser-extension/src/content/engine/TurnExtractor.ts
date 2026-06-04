export class TurnExtractor {
  static extract(node: Element): string {
    const text = (node as HTMLElement).innerText || node.textContent || '';
    return TurnExtractor.clean(text);
  }

  private static clean(text: string): string {
    return text
      .replace(/ /g, ' ')    // non-breaking spaces → regular spaces
      .replace(/[\r\t]+/g, ' ')   // carriage returns and tabs → spaces
      .replace(/\n{3,}/g, '\n\n') // 3+ newlines → double newline
      .trim();
  }
}
