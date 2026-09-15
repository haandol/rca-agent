export default defineEventHandler(async (event) => {
  return usePlaybookLibrary().list(getQuery(event));
});
