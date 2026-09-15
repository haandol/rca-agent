import { createServer } from 'node:http2';

// Bedrock's SDK uses HTTP/2. Keep its fictional embedding endpoint separate from
// Moto's HTTP/1.1 storage endpoint; neither server calls an external provider.
const server = createServer();
server.on('stream', (stream, headers) => {
  const path = String(headers[':path']);
  if (!path.startsWith('/model/') || !path.endsWith('/invoke')) {
    stream.respond({ ':status': 404 });
    stream.end();
    return;
  }
  stream.respond({ ':status': 200, 'content-type': 'application/json' });
  stream.end(JSON.stringify({ embeddings: { float: [[0.1, 0.2, 0.3]] } }));
});
server.listen(4570, '127.0.0.1', () => {
  console.log('Fictional HTTP/2 embeddings: http://127.0.0.1:4570');
});
