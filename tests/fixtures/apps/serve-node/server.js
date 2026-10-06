const http = require('node:http');
if (!process.env.DEMO_TOKEN) throw new Error('DEMO_TOKEN is required');
http.createServer((req, res) => {
  res.setHeader('Content-Type', 'application/json');
  if (req.url === '/health') {
    res.end(JSON.stringify({ status: 'ok', message: 'AI plan deployed!' }));
  } else {
    res.statusCode = 404;
    res.end(JSON.stringify({ message: 'Try /health' }));
  }
}).listen(8087, '0.0.0.0');
