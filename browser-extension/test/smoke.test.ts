describe('smoke', () => {
  it('jsdom provides document', () => {
    expect(document).toBeDefined();
    expect(document.body).toBeDefined();
  });
});
