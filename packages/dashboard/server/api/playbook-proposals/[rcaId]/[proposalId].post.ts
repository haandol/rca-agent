export default defineEventHandler(async (event) => {
  const engine = getQuery(event).engine;
  const body = await readBody<{ action?: string }>(event);
  return usePlaybookLibrary().act(
    getRouterParam(event, 'rcaId') || '',
    typeof engine === 'string' ? engine : '',
    getRouterParam(event, 'proposalId') || '',
    body?.action || '',
  );
});
