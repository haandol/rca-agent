export default defineEventHandler(async (event) => {
  const engine = getQuery(event).engine;
  return usePlaybookLibrary().readProposal(
    getRouterParam(event, 'rcaId') || '',
    typeof engine === 'string' ? engine : '',
  );
});
