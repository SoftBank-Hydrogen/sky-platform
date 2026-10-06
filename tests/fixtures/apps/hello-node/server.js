const http = require('node:http');
const port = Number(process.env.PORT || 3000);
http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  res.end(JSON.stringify(req.url === '/health'
    ? { status: 'ok' }
    : { version: 'v1', message: 'Hello from Sky!' }));
}).listen(port, '0.0.0.0');
