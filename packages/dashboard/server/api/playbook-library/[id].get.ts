export default defineEventHandler(async (event) => {
  const query = getQuery(event);
  return usePlaybookLibrary().detail(getRouterParam(event, 'id') || '', {
    source_rca_id:
      typeof query.source_rca_id === 'string' ? query.source_rca_id : undefined,
    engine: typeof query.engine === 'string' ? query.engine : undefined,
  });
});
